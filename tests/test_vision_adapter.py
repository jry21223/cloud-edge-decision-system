import io

from PIL import Image, ImageDraw

from common.schemas import TaskRequest
from common.vision import (
    ClassicalVisionAdapter,
    crop_roi,
    decode_image,
    image_envelope_from_bytes,
)


def _synthetic_anomaly_png_bytes() -> bytes:
    """Return a generated contract fixture, not real industrial data."""

    image = Image.new("RGB", (96, 72), (128, 128, 128))
    drawing = ImageDraw.Draw(image)
    for y in range(0, image.height, 8):
        drawing.rectangle(
            (0, y, image.width - 1, min(y + 3, image.height - 1)),
            fill=(118, 118, 118),
        )
    drawing.rectangle((30, 20, 55, 40), fill=(250, 250, 250))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _synthetic_task(raw: bytes, *, task_id: str) -> TaskRequest:
    return TaskRequest(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        scene="industrial",
        workpiece_id=f"synthetic-workpiece-{task_id}",
        station_id="synthetic-test-station",
        payload={},
        context={"data_provenance": "synthetic_test_fixture"},
        image=image_envelope_from_bytes(
            raw,
            frame_id=f"synthetic-frame-{task_id}",
            mime_type="image/png",
            local_ref=f"synthetic://{task_id}",
        ),
    )


def test_classical_adapter_localizes_anomaly_in_synthetic_image_bytes():
    task = _synthetic_task(
        _synthetic_anomaly_png_bytes(),
        task_id="adapter-localized-anomaly",
    )

    result = ClassicalVisionAdapter().infer(task, node_id="synthetic-edge")

    assert result.prediction == "unknown_anomaly"
    assert result.action == "isolate"
    assert result.image_quality is not None and result.image_quality.passed
    assert result.anomaly_score is not None and result.anomaly_score >= 0.32
    assert "baseline" in result.model_name
    assert result.model_version == "v1"
    assert result.preprocess_version != "none"
    assert len(result.detections) == 1
    detection = result.detections[0]
    assert detection.label == "unknown_anomaly"
    assert detection.bbox.x <= 30
    assert detection.bbox.y <= 20
    assert detection.bbox.x + detection.bbox.width >= 56
    assert detection.bbox.y + detection.bbox.height >= 41


def test_classical_adapter_quarantines_featureless_synthetic_image():
    image = Image.new("RGB", (64, 64), (128, 128, 128))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    task = _synthetic_task(buffer.getvalue(), task_id="adapter-quality-gate")

    result = ClassicalVisionAdapter().infer(task, node_id="synthetic-edge")

    assert result.prediction == "low_quality"
    assert result.action == "quarantine"
    assert result.image_quality is not None and not result.image_quality.passed
    assert "low_contrast" in result.image_quality.reasons
    assert result.detections == []


def test_roi_crop_produces_a_smaller_decodable_synthetic_artifact():
    task = _synthetic_task(_synthetic_anomaly_png_bytes(), task_id="adapter-roi")
    result = ClassicalVisionAdapter().infer(task, node_id="synthetic-edge")
    assert task.image is not None
    assert result.detections

    roi = crop_roi(task.image, result.detections[0].bbox, margin_ratio=0.10)
    decoded = decode_image(roi)

    assert roi.data_base64 is not None
    assert decoded.size == (roi.width, roi.height)
    assert roi.width < task.image.width
    assert roi.height < task.image.height
    assert roi.byte_size < task.image.byte_size
    assert roi.sha256 != task.image.sha256
    assert roi.local_ref is not None and roi.local_ref.startswith("roi:")
