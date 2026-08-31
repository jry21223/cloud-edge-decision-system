from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import numpy as np
from PIL import Image

from common.industrial_vision import AnomalyObservation, IndustrialDefectLabel
from common.schemas import ImageRegion, VisionDetection
from common.vision import VisionInputError


class OnnxSessionBoundary(Protocol):
    def run(
        self,
        output_names: list[str],
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]: ...


def _image_tensor(image: Image.Image, input_size: int) -> np.ndarray:
    resized = image.resize((input_size, input_size), Image.Resampling.BILINEAR)
    array = np.asarray(resized, dtype=np.float32) / np.float32(255.0)
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...])


class OnnxYoloKnownDefectModel:
    """YOLO ONNX runner for exports with end-to-end NMS output ``[N, 6]``.

    Each row must be ``x1, y1, x2, y2, confidence, class_id`` in resized-input
    coordinates. Export-specific decoding and NMS stay outside the service.
    """

    name = "yolo-onnx-known-defects"
    preprocess_version = "rgb-stretch-0-1-nchw-v1"

    def __init__(
        self,
        *,
        session: OnnxSessionBoundary,
        input_name: str,
        output_name: str,
        input_size: int,
        labels: Sequence[IndustrialDefectLabel],
        version: str,
        score_threshold: float = 0.25,
    ) -> None:
        self.session = session
        self.input_name = input_name
        self.output_name = output_name
        self.input_size = input_size
        self.labels = tuple(labels)
        self.version = version
        self.score_threshold = max(0.0, min(1.0, score_threshold))

    def infer(self, image: Image.Image) -> list[VisionDetection]:
        output = self.session.run(
            [self.output_name],
            {self.input_name: _image_tensor(image, self.input_size)},
        )[0]
        rows = np.asarray(output, dtype=np.float32).reshape(-1, 6)
        scale_x = image.width / self.input_size
        scale_y = image.height / self.input_size
        detections: list[VisionDetection] = []
        for x1, y1, x2, y2, score, raw_class_id in rows:
            confidence = round(float(score), 6)
            if confidence < self.score_threshold:
                continue
            class_id = int(raw_class_id)
            if class_id < 0 or class_id >= len(self.labels):
                raise VisionInputError(f"YOLO class_id {class_id} is outside the frozen labels")
            left = max(0, min(image.width - 1, math.floor(float(x1) * scale_x)))
            top = max(0, min(image.height - 1, math.floor(float(y1) * scale_y)))
            right = max(left + 1, min(image.width, math.ceil(float(x2) * scale_x)))
            bottom = max(top + 1, min(image.height, math.ceil(float(y2) * scale_y)))
            detections.append(
                VisionDetection(
                    label=self.labels[class_id],
                    score=confidence,
                    bbox=ImageRegion(
                        x=left,
                        y=top,
                        width=right - left,
                        height=bottom - top,
                    ),
                    severity="high" if confidence >= 0.85 else "medium",
                )
            )
        return sorted(detections, key=lambda item: item.score, reverse=True)


class OnnxEfficientAdModel:
    """EfficientAD ONNX runner for a scalar score plus pixel anomaly map export."""

    name = "efficientad-onnx-anomaly"
    preprocess_version = "rgb-stretch-0-1-nchw-v1"

    def __init__(
        self,
        *,
        session: OnnxSessionBoundary,
        input_name: str,
        score_output_name: str,
        map_output_name: str,
        input_size: int,
        map_threshold: float,
        version: str,
    ) -> None:
        self.session = session
        self.input_name = input_name
        self.score_output_name = score_output_name
        self.map_output_name = map_output_name
        self.input_size = input_size
        self.map_threshold = max(0.0, min(1.0, map_threshold))
        self.version = version

    def infer(self, image: Image.Image) -> AnomalyObservation:
        score_output, map_output = self.session.run(
            [self.score_output_name, self.map_output_name],
            {self.input_name: _image_tensor(image, self.input_size)},
        )
        score_values = np.asarray(score_output, dtype=np.float32).reshape(-1)
        if score_values.size != 1:
            raise VisionInputError("EfficientAD anomaly_score output must contain one value")
        score = round(float(score_values[0]), 6)
        anomaly_map = np.asarray(map_output, dtype=np.float32).squeeze()
        if anomaly_map.ndim != 2:
            raise VisionInputError("EfficientAD anomaly_map output must be two-dimensional")
        points = np.argwhere(anomaly_map >= self.map_threshold)
        if not len(points):
            return AnomalyObservation(score=score)

        top_index, left_index = points.min(axis=0)
        bottom_index, right_index = points.max(axis=0) + 1
        map_height, map_width = anomaly_map.shape
        left = max(0, math.floor(int(left_index) * image.width / map_width))
        top = max(0, math.floor(int(top_index) * image.height / map_height))
        right = min(image.width, math.ceil(int(right_index) * image.width / map_width))
        bottom = min(image.height, math.ceil(int(bottom_index) * image.height / map_height))
        return AnomalyObservation(
            score=score,
            bbox=ImageRegion(
                x=left,
                y=top,
                width=max(1, right - left),
                height=max(1, bottom - top),
            ),
        )
