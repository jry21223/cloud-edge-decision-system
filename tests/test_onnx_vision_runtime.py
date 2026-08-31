import io

import numpy as np
from PIL import Image

from common.industrial_vision import IndustrialDefectLabel
from common.onnx_vision import OnnxEfficientAdModel, OnnxYoloKnownDefectModel


class SessionBoundary:
    def __init__(self, outputs: list[np.ndarray]) -> None:
        self.outputs = outputs
        self.last_output_names: list[str] | None = None
        self.last_input: dict[str, np.ndarray] | None = None

    def run(
        self,
        output_names: list[str],
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        self.last_output_names = output_names
        self.last_input = input_feed
        return self.outputs


def _image() -> Image.Image:
    buffer = io.BytesIO()
    Image.new("RGB", (100, 50), (128, 128, 128)).save(buffer, format="PNG")
    return Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")


def test_end_to_end_yolo_onnx_output_maps_back_to_original_image_coordinates():
    session = SessionBoundary(
        [np.asarray([[[10, 10, 50, 30, 0.90, 0]]], dtype=np.float32)]
    )
    model = OnnxYoloKnownDefectModel(
        session=session,
        input_name="images",
        output_name="detections",
        input_size=100,
        labels=[IndustrialDefectLabel.SCRATCH],
        version="yolo-fixture-v1",
    )

    detections = model.infer(_image())

    assert len(detections) == 1
    assert detections[0].label == IndustrialDefectLabel.SCRATCH
    assert detections[0].score == 0.90
    assert detections[0].bbox.model_dump() == {"x": 10, "y": 5, "width": 40, "height": 10}
    assert session.last_output_names == ["detections"]
    assert session.last_input is not None
    assert session.last_input["images"].shape == (1, 3, 100, 100)


def test_efficientad_onnx_score_and_map_produce_an_original_image_roi():
    anomaly_map = np.zeros((1, 1, 10, 10), dtype=np.float32)
    anomaly_map[0, 0, 2:5, 3:7] = 0.90
    session = SessionBoundary(
        [np.asarray([0.82], dtype=np.float32), anomaly_map]
    )
    model = OnnxEfficientAdModel(
        session=session,
        input_name="images",
        score_output_name="anomaly_score",
        map_output_name="anomaly_map",
        input_size=100,
        map_threshold=0.60,
        version="efficientad-fixture-v1",
    )

    observation = model.infer(_image())

    assert observation.score == 0.82
    assert observation.bbox is not None
    assert observation.bbox.model_dump() == {"x": 30, "y": 10, "width": 40, "height": 15}
