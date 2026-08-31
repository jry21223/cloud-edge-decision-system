import io
import json

import httpx
import pytest
from PIL import Image, ImageDraw

from common.cloud_vision import OpenAICompatibleVisionReviewAdapter
from common.industrial_vision import IndustrialDefectLabel
from common.schemas import TaskRequest
from common.vision import VisionInputError, image_envelope_from_bytes


def _industrial_task() -> TaskRequest:
    image = Image.new("RGB", (96, 72), (125, 125, 125))
    drawing = ImageDraw.Draw(image)
    drawing.line((10, 36, 82, 36), fill=(240, 240, 240), width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    return TaskRequest(
        task_id="cloud-review-001",
        scene="industrial",
        workpiece_id="part-001",
        station_id="surface-camera-a",
        payload={},
        context={"workpiece_type": "machined-metal-bracket", "material": "metal"},
        image=image_envelope_from_bytes(raw, frame_id="frame-001", mime_type="image/png"),
    )


def _client_with_review(review: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        submitted = json.loads(request.content)
        assert submitted["model"] == "vision-review-model"
        assert submitted["response_format"]["type"] == "json_object"
        user_content = submitted["messages"][1]["content"]
        assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(review)}}]},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cloud_vlm_review_is_constrained_to_the_frozen_defect_contract():
    client = _client_with_review(
        {
            "defect_label": "crack",
            "confidence": 0.93,
            "severity": "critical",
            "bbox": [10, 20, 40, 12],
            "explanation": "linear discontinuity",
            "suggested_action": "continue",
        }
    )
    adapter = OpenAICompatibleVisionReviewAdapter(
        endpoint="https://vlm.example.test/v1/chat/completions",
        model="vision-review-model",
        api_key="test-key",
        data_export_approved=True,
        client=client,
    )

    result = adapter.infer(_industrial_task(), node_id="cloud-node")

    assert result.prediction == IndustrialDefectLabel.CRACK
    assert result.confidence == 0.93
    assert result.action == "quarantine"
    assert result.detections[0].severity == "critical"
    assert result.detections[0].bbox.width == 40
    assert "continue" not in result.reason


def test_cloud_vlm_rejects_labels_outside_the_agreed_taxonomy():
    client = _client_with_review(
        {
            "defect_label": "rust",
            "confidence": 0.80,
            "severity": "medium",
            "bbox": [1, 1, 10, 10],
            "explanation": "unsupported label",
        }
    )
    adapter = OpenAICompatibleVisionReviewAdapter(
        endpoint="https://vlm.example.test/v1/chat/completions",
        model="vision-review-model",
        api_key="test-key",
        data_export_approved=True,
        client=client,
    )

    with pytest.raises(VisionInputError, match="structured review"):
        adapter.infer(_industrial_task(), node_id="cloud-node")


def test_cloud_vlm_requires_https_for_external_image_export():
    with pytest.raises(ValueError, match="must use HTTPS"):
        OpenAICompatibleVisionReviewAdapter(
            endpoint="http://vlm.example.test/v1/chat/completions",
            model="vision-review-model",
            api_key="test-key",
            data_export_approved=True,
        )


def test_cloud_vlm_rejects_an_out_of_bounds_bbox():
    client = _client_with_review(
        {
            "defect_label": "scratch",
            "confidence": 0.80,
            "severity": "medium",
            "bbox": [90, 60, 20, 20],
            "explanation": "outside image",
        }
    )
    adapter = OpenAICompatibleVisionReviewAdapter(
        endpoint="https://vlm.example.test/v1/chat/completions",
        model="vision-review-model",
        api_key="test-key",
        data_export_approved=True,
        client=client,
    )

    with pytest.raises(VisionInputError, match="bbox is outside"):
        adapter.infer(_industrial_task(), node_id="cloud-node")


def test_cloud_vlm_rejects_invalid_json_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json"}}]},
            request=request,
        )

    adapter = OpenAICompatibleVisionReviewAdapter(
        endpoint="https://vlm.example.test/v1/chat/completions",
        model="vision-review-model",
        api_key="test-key",
        data_export_approved=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(VisionInputError, match="invalid structured review"):
        adapter.infer(_industrial_task(), node_id="cloud-node")


def test_cloud_vlm_propagates_timeout_and_5xx_boundary_failures():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("cloud timeout", request=request)

    timeout_adapter = OpenAICompatibleVisionReviewAdapter(
        endpoint="https://vlm.example.test/v1/chat/completions",
        model="vision-review-model",
        api_key="test-key",
        data_export_approved=True,
        client=httpx.Client(transport=httpx.MockTransport(timeout_handler)),
    )
    with pytest.raises(httpx.ReadTimeout):
        timeout_adapter.infer(_industrial_task(), node_id="cloud-node")

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    error_adapter = OpenAICompatibleVisionReviewAdapter(
        endpoint="https://vlm.example.test/v1/chat/completions",
        model="vision-review-model",
        api_key="test-key",
        data_export_approved=True,
        client=httpx.Client(transport=httpx.MockTransport(error_handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        error_adapter.infer(_industrial_task(), node_id="cloud-node")
