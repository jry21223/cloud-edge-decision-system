from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from common.industrial_vision import IndustrialDefectLabel
from common.schemas import ImageRegion, InferenceResult, TaskRequest, VisionDetection
from common.vision import VisionInputError


class CloudReviewBox(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def parse_coordinates(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return dict(zip(("x", "y", "width", "height"), value, strict=True))
        return value


class StructuredCloudReview(BaseModel):
    defect_label: IndustrialDefectLabel
    confidence: float = Field(ge=0.0, le=1.0)
    severity: str
    bbox: CloudReviewBox
    explanation: str = Field(min_length=1, max_length=1000)
    suggested_action: str | None = Field(default=None, max_length=128)


class OpenAICompatibleVisionReviewAdapter:
    """Cloud VLM review with a strict defect schema and deterministic action mapping."""

    version = "v1"
    preprocess_version = "inline-image-structured-json-v1"

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        data_export_approved: bool,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
            raise ValueError("cloud VLM endpoint must be an HTTP(S) URL")
        if parsed_endpoint.scheme != "https" and parsed_endpoint.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("external cloud VLM endpoint must use HTTPS")
        if not model:
            raise ValueError("cloud VLM model is required")
        if not api_key:
            raise ValueError("cloud VLM API key is required")
        if not data_export_approved:
            raise ValueError("cloud VLM image export requires explicit approval")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.client = client or httpx.Client()
        self.timeout_seconds = timeout_seconds
        self.name = f"vlm:{model}"

    def infer(self, task: TaskRequest, *, node_id: str) -> InferenceResult:
        started = time.perf_counter()
        if task.scene != "industrial" or task.image is None:
            raise VisionInputError("cloud VLM review requires an industrial image task")
        if task.image.data_base64 is None:
            raise VisionInputError("cloud VLM review requires inline image bytes")

        response = self.client.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=self._request_body(task),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        review = self._parse_review(response)
        bbox = review.bbox
        if (
            bbox.x + bbox.width > task.image.width
            or bbox.y + bbox.height > task.image.height
        ):
            raise VisionInputError("cloud VLM structured review bbox is outside the image")

        detection = VisionDetection(
            label=review.defect_label,
            score=review.confidence,
            bbox=ImageRegion.model_validate(bbox.model_dump()),
            severity=self._severity(review.severity),
        )
        return InferenceResult(
            prediction=review.defect_label,
            confidence=review.confidence,
            action="quarantine",
            reason=f"Cloud structured visual review: {review.explanation}",
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            model_name=self.name,
            node_id=node_id,
            model_version=self.version,
            preprocess_version=self.preprocess_version,
            detections=[detection],
        )

    def _request_body(self, task: TaskRequest) -> dict[str, object]:
        assert task.image is not None and task.image.data_base64 is not None
        labels = ", ".join(label.value for label in IndustrialDefectLabel)
        return {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Review a metal industrial component image. Return one JSON object with "
                        "defect_label, confidence, severity, bbox=[x,y,width,height], explanation. "
                        f"defect_label must be one of: {labels}. Do not choose the production action."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "workpiece_id": task.workpiece_id,
                                    "workpiece_type": task.context.get("workpiece_type"),
                                    "material": task.context.get("material"),
                                },
                                ensure_ascii=False,
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    f"data:{task.image.mime_type};base64,"
                                    f"{task.image.data_base64}"
                                )
                            },
                        },
                    ],
                },
            ],
        }

    @staticmethod
    def _parse_review(response: httpx.Response) -> StructuredCloudReview:
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not a string")
            return StructuredCloudReview.model_validate_json(content)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as error:
            raise VisionInputError("cloud VLM returned an invalid structured review") from error

    @staticmethod
    def _severity(value: str) -> str:
        normalized = value.lower()
        if normalized not in {"low", "medium", "high", "critical"}:
            raise VisionInputError("cloud VLM structured review has invalid severity")
        return normalized
