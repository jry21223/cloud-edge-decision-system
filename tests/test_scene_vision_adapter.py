import io

from PIL import Image, ImageDraw

from common.scene_vision import SceneVisionAdapter, TrafficVisionMigrationAdapter
from common.schemas import TaskRequest
from common.vision import ClassicalVisionAdapter, image_envelope_from_bytes


def _image_task(scene: str) -> TaskRequest:
    image = Image.new("RGB", (96, 72), (125, 125, 125))
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((8, 8, 40, 28), fill=(180, 180, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    return TaskRequest(
        task_id=f"{scene}-vision-probe",
        scene=scene,
        payload={},
        context={"data_provenance": "synthetic_architecture_probe"},
        image=image_envelope_from_bytes(raw, frame_id=f"{scene}-frame", mime_type="image/png"),
    )


def test_traffic_visual_task_only_proves_adapter_migration_without_claiming_detection():
    adapter = SceneVisionAdapter(
        industrial=ClassicalVisionAdapter(),
        traffic=TrafficVisionMigrationAdapter(),
    )

    result = adapter.infer(_image_task("traffic"), node_id="edge-a")

    assert result.prediction == "traffic_visual_review_required"
    assert result.action == "observe"
    assert result.confidence == 0.50
    assert result.detections == []
    assert result.model_name == "traffic-vision-migration-probe"
    assert "architecture migration only" in result.reason


def test_scene_router_preserves_the_existing_industrial_adapter_behavior():
    adapter = SceneVisionAdapter(
        industrial=ClassicalVisionAdapter(),
        traffic=TrafficVisionMigrationAdapter(),
    )

    result = adapter.infer(_image_task("industrial"), node_id="edge-a")

    assert result.model_name == "classical-vision-baseline"
