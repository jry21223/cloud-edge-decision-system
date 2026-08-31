import json
from pathlib import Path

import pytest

from common.cloud_vision import OpenAICompatibleVisionReviewAdapter
from common.industrial_vision import YoloEfficientAdAdapter
from common.scene_vision import SceneVisionAdapter, TrafficVisionMigrationAdapter
from common.vision import ClassicalVisionAdapter
from common.vision_runtime import build_cloud_vision_adapter, build_edge_vision_adapter


def test_default_runtime_is_explicit_software_simulation_for_both_scenes():
    edge = build_edge_vision_adapter({})
    cloud = build_cloud_vision_adapter({})

    assert isinstance(edge, SceneVisionAdapter)
    assert isinstance(edge.industrial, ClassicalVisionAdapter)
    assert isinstance(edge.traffic, TrafficVisionMigrationAdapter)
    assert isinstance(cloud.industrial, ClassicalVisionAdapter)
    assert isinstance(cloud.traffic, TrafficVisionMigrationAdapter)


def test_edge_real_mode_loads_the_frozen_profile_and_both_onnx_models(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "workpiece_type": "machined-metal-bracket",
                "material": "metal",
                "defect_labels": [
                    "scratch",
                    "crack",
                    "pit_or_wear",
                    "contamination",
                    "missing_or_assembly",
                ],
            }
        ),
        encoding="utf-8",
    )
    yolo_path = tmp_path / "yolo.onnx"
    efficientad_path = tmp_path / "efficientad.onnx"
    yolo_path.touch()
    efficientad_path.touch()
    opened: list[Path] = []

    def session_factory(path: Path) -> object:
        opened.append(path)
        return object()

    adapter = build_edge_vision_adapter(
        {
            "EDGE_VISION_BACKEND": "yolo_efficientad",
            "INDUSTRIAL_VISION_PROFILE": str(profile_path),
            "YOLO_ONNX_PATH": str(yolo_path),
            "EFFICIENTAD_ONNX_PATH": str(efficientad_path),
        },
        session_factory=session_factory,
    )

    assert isinstance(adapter.industrial, YoloEfficientAdAdapter)
    assert adapter.industrial.profile.workpiece_type == "machined-metal-bracket"
    assert opened == [yolo_path, efficientad_path]


def test_real_model_modes_fail_closed_when_required_configuration_is_missing():
    with pytest.raises(RuntimeError, match="INDUSTRIAL_VISION_PROFILE"):
        build_edge_vision_adapter({"EDGE_VISION_BACKEND": "yolo_efficientad"})

    with pytest.raises(RuntimeError, match="CLOUD_VLM_ENDPOINT"):
        build_cloud_vision_adapter({"CLOUD_VISION_BACKEND": "vlm"})

    with pytest.raises(RuntimeError, match="DATA_EXPORT_APPROVED"):
        build_cloud_vision_adapter(
            {
                "CLOUD_VISION_BACKEND": "vlm",
                "CLOUD_VLM_ENDPOINT": "https://vlm.example.test/v1/chat/completions",
                "CLOUD_VLM_MODEL": "vision-review-model",
                "CLOUD_VLM_API_KEY": "secret",
            }
        )


def test_cloud_vlm_mode_uses_the_structured_review_adapter():
    adapter = build_cloud_vision_adapter(
        {
            "CLOUD_VISION_BACKEND": "vlm",
            "CLOUD_VLM_ENDPOINT": "https://vlm.example.test/v1/chat/completions",
            "CLOUD_VLM_MODEL": "vision-review-model",
            "CLOUD_VLM_API_KEY": "secret",
            "CLOUD_VLM_DATA_EXPORT_APPROVED": "true",
        }
    )

    assert isinstance(adapter.industrial, OpenAICompatibleVisionReviewAdapter)
    assert isinstance(adapter.traffic, TrafficVisionMigrationAdapter)
