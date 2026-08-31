import io

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from common.industrial_vision import (
    AnomalyObservation,
    IndustrialDefectLabel,
    IndustrialVisionProfile,
    YoloEfficientAdAdapter,
)
from common.schemas import ImageRegion, TaskRequest, VisionDetection
from common.vision import VisionInputError, image_envelope_from_bytes

REQUIRED_DEFECTS = [
    IndustrialDefectLabel.SCRATCH,
    IndustrialDefectLabel.CRACK,
    IndustrialDefectLabel.PIT_OR_WEAR,
    IndustrialDefectLabel.CONTAMINATION,
    IndustrialDefectLabel.MISSING_OR_ASSEMBLY,
]


def _featureful_image() -> bytes:
    image = Image.new("RGB", (96, 72), (120, 120, 120))
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((0, 0, 95, 5), fill=(80, 80, 80))
    drawing.rectangle((20, 18, 60, 46), fill=(180, 180, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _task(*, workpiece_type: str = "machined-metal-bracket") -> TaskRequest:
    raw = _featureful_image()
    return TaskRequest(
        task_id="industrial-dual-model",
        scene="industrial",
        workpiece_id="part-001",
        station_id="surface-camera-a",
        payload={},
        context={"workpiece_type": workpiece_type, "material": "metal"},
        image=image_envelope_from_bytes(
            raw,
            frame_id="frame-001",
            mime_type="image/png",
        ),
    )


class ScratchYoloBoundary:
    name = "yolo11n-defect"
    version = "fixture-yolo-v1"
    preprocess_version = "letterbox-rgb-v1"

    def infer(self, _image: Image.Image) -> list[VisionDetection]:
        return [
            VisionDetection(
                label=IndustrialDefectLabel.SCRATCH,
                score=0.91,
                bbox=ImageRegion(x=20, y=18, width=40, height=28),
                severity="high",
            )
        ]


class EmptyYoloBoundary(ScratchYoloBoundary):
    def infer(self, _image: Image.Image) -> list[VisionDetection]:
        return []


class EfficientAdBoundary:
    name = "efficientad-small"
    version = "fixture-efficientad-v1"
    preprocess_version = "imagenet-rgb-v1"

    def __init__(self, score: float) -> None:
        self.score = score

    def infer(self, _image: Image.Image) -> AnomalyObservation:
        return AnomalyObservation(
            score=self.score,
            bbox=ImageRegion(x=18, y=16, width=46, height=34),
        )


def _profile(**updates: object) -> IndustrialVisionProfile:
    values: dict[str, object] = {
        "workpiece_type": "machined-metal-bracket",
        "material": "metal",
        "defect_labels": REQUIRED_DEFECTS,
    }
    values.update(updates)
    return IndustrialVisionProfile.model_validate(values)


def test_profile_requires_the_five_agreed_metal_defect_classes():
    with pytest.raises(ValidationError, match="exactly the agreed five defect classes"):
        _profile(defect_labels=REQUIRED_DEFECTS[:-1])

    with pytest.raises(ValidationError, match="material must be metal"):
        _profile(material="plastic")


def test_known_yolo_defect_produces_a_reject_decision_at_the_public_adapter_seam():
    adapter = YoloEfficientAdAdapter(
        profile=_profile(),
        known_defect_model=ScratchYoloBoundary(),
        anomaly_model=EfficientAdBoundary(0.48),
    )

    result = adapter.infer(_task(), node_id="edge-a")

    assert result.prediction == IndustrialDefectLabel.SCRATCH
    assert result.action == "reject"
    assert result.confidence == 0.91
    assert result.anomaly_score == 0.48
    assert [item.label for item in result.detections] == [IndustrialDefectLabel.SCRATCH]
    assert result.model_name == "yolo11n-defect+efficientad-small"


def test_efficientad_unknown_anomaly_is_quarantined_when_yolo_has_no_known_class():
    adapter = YoloEfficientAdAdapter(
        profile=_profile(),
        known_defect_model=EmptyYoloBoundary(),
        anomaly_model=EfficientAdBoundary(0.82),
        anomaly_threshold=0.60,
    )

    result = adapter.infer(_task(), node_id="edge-a")

    assert result.prediction == IndustrialDefectLabel.UNKNOWN_ANOMALY
    assert result.action == "quarantine"
    assert result.confidence == 0.82
    assert result.detections[0].label == IndustrialDefectLabel.UNKNOWN_ANOMALY


def test_real_model_profile_rejects_the_wrong_workpiece_before_inference():
    adapter = YoloEfficientAdAdapter(
        profile=_profile(),
        known_defect_model=ScratchYoloBoundary(),
        anomaly_model=EfficientAdBoundary(0.10),
    )

    with pytest.raises(VisionInputError, match="workpiece_type"):
        adapter.infer(_task(workpiece_type="cast-metal-gear"), node_id="edge-a")
