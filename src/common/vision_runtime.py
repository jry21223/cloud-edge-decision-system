from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from common.cloud_vision import OpenAICompatibleVisionReviewAdapter
from common.industrial_vision import IndustrialVisionProfile, YoloEfficientAdAdapter
from common.onnx_vision import (
    OnnxEfficientAdModel,
    OnnxSessionBoundary,
    OnnxYoloKnownDefectModel,
)
from common.scene_vision import SceneVisionAdapter, TrafficVisionMigrationAdapter
from common.vision import ClassicalVisionAdapter

SessionFactory = Callable[[Path], OnnxSessionBoundary]


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for the selected vision backend")
    return value


def _existing_file(environment: Mapping[str, str], name: str) -> Path:
    path = Path(_required(environment, name)).expanduser()
    if not path.is_file():
        raise RuntimeError(f"{name} does not point to a readable file: {path}")
    return path


def _onnx_session(path: Path) -> OnnxSessionBoundary:
    try:
        import onnxruntime
    except ImportError as error:
        raise RuntimeError(
            "onnxruntime is required for EDGE_VISION_BACKEND=yolo_efficientad"
        ) from error
    return onnxruntime.InferenceSession(
        str(path),
        providers=["CPUExecutionProvider"],
    )


def build_edge_vision_adapter(
    environment: Mapping[str, str] | None = None,
    *,
    session_factory: SessionFactory | None = None,
) -> SceneVisionAdapter:
    env = os.environ if environment is None else environment
    backend = env.get("EDGE_VISION_BACKEND", "simulation").strip().lower()
    traffic = TrafficVisionMigrationAdapter()
    if backend == "simulation":
        return SceneVisionAdapter(
            industrial=ClassicalVisionAdapter(
                name="classical-edge-simulation",
                version="v1",
            ),
            traffic=traffic,
        )
    if backend != "yolo_efficientad":
        raise RuntimeError(f"unsupported EDGE_VISION_BACKEND: {backend}")

    profile_path = _existing_file(env, "INDUSTRIAL_VISION_PROFILE")
    yolo_path = _existing_file(env, "YOLO_ONNX_PATH")
    efficientad_path = _existing_file(env, "EFFICIENTAD_ONNX_PATH")
    try:
        profile = IndustrialVisionProfile.model_validate_json(
            profile_path.read_text(encoding="utf-8")
        )
    except ValueError as error:
        raise RuntimeError(f"invalid INDUSTRIAL_VISION_PROFILE: {profile_path}") from error

    make_session = session_factory or _onnx_session
    yolo = OnnxYoloKnownDefectModel(
        session=make_session(yolo_path),
        input_name=env.get("YOLO_INPUT_NAME", "images"),
        output_name=env.get("YOLO_OUTPUT_NAME", "detections"),
        input_size=int(env.get("YOLO_INPUT_SIZE", "640")),
        labels=profile.defect_labels,
        version=env.get("YOLO_MODEL_VERSION", yolo_path.stem),
        score_threshold=float(env.get("YOLO_SCORE_THRESHOLD", "0.50")),
    )
    efficientad = OnnxEfficientAdModel(
        session=make_session(efficientad_path),
        input_name=env.get("EFFICIENTAD_INPUT_NAME", "images"),
        score_output_name=env.get("EFFICIENTAD_SCORE_OUTPUT", "anomaly_score"),
        map_output_name=env.get("EFFICIENTAD_MAP_OUTPUT", "anomaly_map"),
        input_size=int(env.get("EFFICIENTAD_INPUT_SIZE", "256")),
        map_threshold=float(env.get("EFFICIENTAD_MAP_THRESHOLD", "0.60")),
        version=env.get("EFFICIENTAD_MODEL_VERSION", efficientad_path.stem),
    )
    industrial = YoloEfficientAdAdapter(
        profile=profile,
        known_defect_model=yolo,
        anomaly_model=efficientad,
        known_defect_threshold=float(env.get("YOLO_SCORE_THRESHOLD", "0.50")),
        anomaly_threshold=float(env.get("EFFICIENTAD_SCORE_THRESHOLD", "0.60")),
    )
    return SceneVisionAdapter(industrial=industrial, traffic=traffic)


def build_cloud_vision_adapter(
    environment: Mapping[str, str] | None = None,
) -> SceneVisionAdapter:
    env = os.environ if environment is None else environment
    backend = env.get("CLOUD_VISION_BACKEND", "simulation").strip().lower()
    traffic = TrafficVisionMigrationAdapter()
    if backend == "simulation":
        return SceneVisionAdapter(
            industrial=ClassicalVisionAdapter(
                anomaly_threshold=0.24,
                name="classical-cloud-simulation",
                version="v1",
            ),
            traffic=traffic,
        )
    if backend != "vlm":
        raise RuntimeError(f"unsupported CLOUD_VISION_BACKEND: {backend}")
    endpoint = _required(env, "CLOUD_VLM_ENDPOINT")
    model = _required(env, "CLOUD_VLM_MODEL")
    api_key = _required(env, "CLOUD_VLM_API_KEY")
    data_export_approved = env.get("CLOUD_VLM_DATA_EXPORT_APPROVED", "false").lower() == "true"
    if not data_export_approved:
        raise RuntimeError(
            "CLOUD_VLM_DATA_EXPORT_APPROVED=true is required before image export"
        )
    industrial = OpenAICompatibleVisionReviewAdapter(
        endpoint=endpoint,
        model=model,
        api_key=api_key,
        data_export_approved=data_export_approved,
        timeout_seconds=float(env.get("CLOUD_VLM_TIMEOUT_SECONDS", "20")),
    )
    return SceneVisionAdapter(industrial=industrial, traffic=traffic)
