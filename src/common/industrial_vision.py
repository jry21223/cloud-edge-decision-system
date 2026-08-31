from __future__ import annotations

import time
from enum import StrEnum
from typing import Protocol

from PIL import Image
from pydantic import BaseModel, Field, model_validator

from common.schemas import ImageRegion, InferenceResult, TaskRequest, VisionDetection
from common.vision import ClassicalVisionAdapter, VisionInputError, decode_image


class IndustrialDefectLabel(StrEnum):
    SCRATCH = "scratch"
    CRACK = "crack"
    PIT_OR_WEAR = "pit_or_wear"
    CONTAMINATION = "contamination"
    MISSING_OR_ASSEMBLY = "missing_or_assembly"
    UNKNOWN_ANOMALY = "unknown_anomaly"


AGREED_DEFECT_LABELS = frozenset(
    {
        IndustrialDefectLabel.SCRATCH,
        IndustrialDefectLabel.CRACK,
        IndustrialDefectLabel.PIT_OR_WEAR,
        IndustrialDefectLabel.CONTAMINATION,
        IndustrialDefectLabel.MISSING_OR_ASSEMBLY,
    }
)


class IndustrialVisionProfile(BaseModel):
    """Frozen business scope for one metal workpiece and its known defects."""

    workpiece_type: str = Field(min_length=1, max_length=128)
    material: str = Field(min_length=1, max_length=64)
    defect_labels: list[IndustrialDefectLabel]

    @model_validator(mode="after")
    def validate_scope(self) -> IndustrialVisionProfile:
        if self.material != "metal":
            raise ValueError("material must be metal")
        if set(self.defect_labels) != AGREED_DEFECT_LABELS or len(self.defect_labels) != len(
            AGREED_DEFECT_LABELS
        ):
            raise ValueError("defect_labels must contain exactly the agreed five defect classes")
        return self


class AnomalyObservation(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    bbox: ImageRegion | None = None


class KnownDefectModel(Protocol):
    name: str
    version: str
    preprocess_version: str

    def infer(self, image: Image.Image) -> list[VisionDetection]: ...


class AnomalyModel(Protocol):
    name: str
    version: str
    preprocess_version: str

    def infer(self, image: Image.Image) -> AnomalyObservation: ...


class YoloEfficientAdAdapter:
    """Fuse a YOLO known-defect detector with an EfficientAD anomaly detector.

    Model runtimes live behind the two narrow protocols so ONNX/OpenVINO/TensorRT
    implementations can change without changing the public ``infer`` contract.
    """

    name = "yolo+efficientad"

    def __init__(
        self,
        *,
        profile: IndustrialVisionProfile,
        known_defect_model: KnownDefectModel,
        anomaly_model: AnomalyModel,
        known_defect_threshold: float = 0.50,
        anomaly_threshold: float = 0.60,
    ) -> None:
        self.profile = profile
        self.known_defect_model = known_defect_model
        self.anomaly_model = anomaly_model
        self.known_defect_threshold = max(0.0, min(1.0, known_defect_threshold))
        self.anomaly_threshold = max(0.0, min(1.0, anomaly_threshold))
        self.name = f"{known_defect_model.name}+{anomaly_model.name}"
        self.version = f"{known_defect_model.version}+{anomaly_model.version}"
        self.preprocess_version = (
            f"{known_defect_model.preprocess_version}+{anomaly_model.preprocess_version}"
        )

    def infer(self, task: TaskRequest, *, node_id: str) -> InferenceResult:
        started = time.perf_counter()
        self._validate_task_scope(task)
        if task.image is None:
            raise VisionInputError("industrial vision adapter requires task.image")

        image = decode_image(task.image)
        quality_result = ClassicalVisionAdapter().infer(task, node_id=node_id)
        if quality_result.image_quality is not None and not quality_result.image_quality.passed:
            return quality_result.model_copy(
                update={
                    "model_name": self.name,
                    "model_version": self.version,
                    "preprocess_version": self.preprocess_version,
                }
            )

        detections = self.known_defect_model.infer(image)
        allowed = {label.value for label in self.profile.defect_labels}
        unexpected = sorted({str(item.label) for item in detections} - allowed)
        if unexpected:
            raise VisionInputError(f"YOLO returned labels outside the frozen profile: {unexpected}")
        anomaly = self.anomaly_model.infer(image)
        selected = max(detections, key=lambda item: item.score, default=None)

        if selected is not None and selected.score >= self.known_defect_threshold:
            prediction = selected.label
            action = "reject"
            confidence = selected.score
            reason = "YOLO detected a known defect from the frozen metal-workpiece taxonomy"
        elif anomaly.score >= self.anomaly_threshold:
            prediction = IndustrialDefectLabel.UNKNOWN_ANOMALY
            action = "quarantine"
            confidence = anomaly.score
            reason = "EfficientAD detected an unknown surface anomaly"
            bbox = anomaly.bbox or ImageRegion(
                x=0,
                y=0,
                width=image.width,
                height=image.height,
            )
            detections = [
                VisionDetection(
                    label=IndustrialDefectLabel.UNKNOWN_ANOMALY,
                    score=anomaly.score,
                    bbox=bbox,
                    severity="high" if anomaly.score >= 0.80 else "medium",
                )
            ]
        else:
            prediction = "normal"
            action = "pass"
            confidence = max(0.0, 1.0 - anomaly.score)
            reason = "Neither YOLO nor EfficientAD exceeded the frozen decision thresholds"
            detections = []

        return InferenceResult(
            prediction=prediction,
            confidence=round(confidence, 6),
            action=action,
            reason=reason,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            model_name=self.name,
            node_id=node_id,
            model_version=self.version,
            preprocess_version=self.preprocess_version,
            detections=detections,
            image_quality=quality_result.image_quality,
            anomaly_score=round(anomaly.score, 6),
            scene_complexity=quality_result.scene_complexity,
        )

    def _validate_task_scope(self, task: TaskRequest) -> None:
        if task.scene != "industrial":
            raise VisionInputError("industrial vision adapter only accepts scene=industrial")
        if task.context.get("material") != self.profile.material:
            raise VisionInputError("task material does not match the frozen metal profile")
        if task.context.get("workpiece_type") != self.profile.workpiece_type:
            raise VisionInputError("task workpiece_type does not match the frozen profile")
