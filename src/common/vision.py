from __future__ import annotations

import base64
import hashlib
import io
import math
from collections.abc import Sequence
from typing import Protocol

from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError

from common.schemas import (
    ImageEnvelope,
    ImageQuality,
    ImageRegion,
    InferenceResult,
    TaskRequest,
    VisionDetection,
)

_MAX_ANALYSIS_SIDE = 512
_MIME_TO_FORMAT = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/bmp": "BMP",
}


class VisionInputError(ValueError):
    """Raised when declared image metadata and decoded bytes disagree."""


class VisionModelAdapter(Protocol):
    name: str
    version: str
    preprocess_version: str

    def infer(self, task: TaskRequest, *, node_id: str) -> InferenceResult: ...


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def decode_image(envelope: ImageEnvelope) -> Image.Image:
    if envelope.data_base64 is None:
        raise VisionInputError("inline image bytes are required on the data plane")
    raw = base64.b64decode(envelope.data_base64, validate=True)
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.verify()
        with Image.open(io.BytesIO(raw)) as opened:
            image = opened.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise VisionInputError("image bytes cannot be decoded") from error
    if image.size != (envelope.width, envelope.height):
        raise VisionInputError(
            f"declared dimensions {envelope.width}x{envelope.height} "
            f"do not match decoded image {image.width}x{image.height}"
        )
    return image


def image_envelope_from_bytes(
    raw: bytes,
    *,
    frame_id: str,
    mime_type: str,
    local_ref: str | None = None,
    roi: ImageRegion | None = None,
) -> ImageEnvelope:
    if mime_type not in _MIME_TO_FORMAT:
        raise VisionInputError(f"unsupported mime type: {mime_type}")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            width, height = opened.size
    except (UnidentifiedImageError, OSError) as error:
        raise VisionInputError("image bytes cannot be decoded") from error
    return ImageEnvelope(
        frame_id=frame_id,
        width=width,
        height=height,
        mime_type=mime_type,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        local_ref=local_ref,
        roi=roi,
        data_base64=base64.b64encode(raw).decode("ascii"),
    )


def crop_roi(
    envelope: ImageEnvelope,
    bbox: ImageRegion,
    *,
    margin_ratio: float = 0.15,
) -> ImageEnvelope:
    """Crop a bounded ROI and return a new, checksummed data-plane envelope."""

    image = decode_image(envelope)
    margin_x = max(2, round(bbox.width * max(0.0, margin_ratio)))
    margin_y = max(2, round(bbox.height * max(0.0, margin_ratio)))
    left = max(0, bbox.x - margin_x)
    top = max(0, bbox.y - margin_y)
    right = min(image.width, bbox.x + bbox.width + margin_x)
    bottom = min(image.height, bbox.y + bbox.height + margin_y)
    cropped = image.crop((left, top, right, bottom))
    buffer = io.BytesIO()
    output_format = "PNG" if envelope.mime_type == "image/png" else "JPEG"
    save_kwargs = {"quality": 90, "optimize": True} if output_format == "JPEG" else {}
    cropped.save(buffer, format=output_format, **save_kwargs)
    crop_bytes = buffer.getvalue()
    crop_region = ImageRegion(x=left, y=top, width=right - left, height=bottom - top)
    return image_envelope_from_bytes(
        crop_bytes,
        frame_id=f"{envelope.frame_id}-roi",
        mime_type="image/png" if output_format == "PNG" else "image/jpeg",
        local_ref=f"roi:{envelope.sha256}:{left},{top},{right},{bottom}",
        roi=ImageRegion(x=0, y=0, width=cropped.width, height=cropped.height),
    ).model_copy(
        update={"local_ref": f"roi:{envelope.sha256}:{crop_region.model_dump_json()}"}
    )


def _median_from_histogram(histogram: Sequence[int], count: int) -> int:
    midpoint = max(0, (count - 1) // 2)
    cumulative = 0
    for value, frequency in enumerate(histogram):
        cumulative += frequency
        if cumulative > midpoint:
            return value
    return 0


def _entropy(histogram: Sequence[int], count: int) -> float:
    if count <= 0:
        return 0.0
    entropy = 0.0
    for frequency in histogram:
        if frequency:
            probability = frequency / count
            entropy -= probability * math.log2(probability)
    return _clamp(entropy / 8.0)


class ClassicalVisionAdapter:
    """Deterministic pixel baseline for exercising the real image byte path.

    This is deliberately labelled a classical baseline, not a trained defect
    model. It provides reproducible quality scores, an anomaly score, and a
    localized bounding box while the team supplies a licensed dataset/model.
    """

    name = "classical-vision-baseline"
    version = "v1"
    preprocess_version = "rgb-gray-512-v1"

    def __init__(
        self,
        *,
        anomaly_threshold: float = 0.32,
        name: str | None = None,
        version: str | None = None,
    ) -> None:
        self.anomaly_threshold = _clamp(anomaly_threshold)
        if name is not None:
            self.name = name
        if version is not None:
            self.version = version

    def infer(self, task: TaskRequest, *, node_id: str) -> InferenceResult:
        import time

        started = time.perf_counter()
        if task.image is None:
            raise VisionInputError("vision adapter requires task.image")
        original = decode_image(task.image)
        analysis = original.copy()
        analysis.thumbnail((_MAX_ANALYSIS_SIDE, _MAX_ANALYSIS_SIDE), Image.Resampling.LANCZOS)
        gray = analysis.convert("L")
        stats = ImageStat.Stat(gray)
        brightness = _clamp(stats.mean[0] / 255.0)
        contrast = _clamp(stats.stddev[0] / 96.0)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        sharpness = _clamp(ImageStat.Stat(edges).mean[0] / 48.0)

        quality_reasons: list[str] = []
        if brightness < 0.06:
            quality_reasons.append("underexposed")
        if brightness > 0.96:
            quality_reasons.append("overexposed")
        if contrast < 0.015:
            quality_reasons.append("low_contrast")
        if sharpness < 0.012:
            quality_reasons.append("blur_or_featureless")
        quality = ImageQuality(
            brightness=round(brightness, 6),
            contrast=round(contrast, 6),
            sharpness=round(sharpness, 6),
            passed=not quality_reasons,
            reasons=quality_reasons,
        )

        histogram = gray.histogram()
        pixel_count = gray.width * gray.height
        median = _median_from_histogram(histogram, pixel_count)
        deviation_threshold = max(32.0, stats.stddev[0] * 2.2)
        pixels = gray.load()
        anomaly_points: list[tuple[int, int]] = []
        deviation_total = 0.0
        for y in range(gray.height):
            for x in range(gray.width):
                deviation = abs(float(pixels[x, y]) - median)
                if deviation >= deviation_threshold:
                    anomaly_points.append((x, y))
                    deviation_total += deviation

        anomaly_ratio = len(anomaly_points) / max(pixel_count, 1)
        mean_deviation = deviation_total / max(len(anomaly_points), 1) / 255.0
        anomaly_score = _clamp(anomaly_ratio * 12.0 + mean_deviation * 0.55)
        scene_complexity = _clamp(0.55 * _entropy(histogram, pixel_count) + 0.45 * sharpness)
        detections: list[VisionDetection] = []

        if anomaly_points:
            xs = [point[0] for point in anomaly_points]
            ys = [point[1] for point in anomaly_points]
            scale_x = original.width / gray.width
            scale_y = original.height / gray.height
            left = max(0, math.floor(min(xs) * scale_x))
            top = max(0, math.floor(min(ys) * scale_y))
            right = min(original.width, math.ceil((max(xs) + 1) * scale_x))
            bottom = min(original.height, math.ceil((max(ys) + 1) * scale_y))
            if right > left and bottom > top:
                detections.append(
                    VisionDetection(
                        label="unknown_anomaly",
                        score=round(anomaly_score, 6),
                        bbox=ImageRegion(
                            x=left,
                            y=top,
                            width=right - left,
                            height=bottom - top,
                        ),
                        severity=("high" if anomaly_score >= 0.75 else "medium"),
                    )
                )

        if not quality.passed:
            prediction = "low_quality"
            action = "quarantine"
            confidence = 0.95
            reason = "图像质量门控失败，禁止将低质量图像判为正常"
        elif anomaly_score >= self.anomaly_threshold and detections:
            prediction = "unknown_anomaly"
            action = "isolate"
            confidence = _clamp(0.55 + anomaly_score * 0.40)
            reason = "经典像素基线定位到偏离背景分布的疑似缺陷区域"
        else:
            prediction = "normal"
            action = "continue"
            confidence = _clamp(0.94 - anomaly_score * 0.75)
            reason = "经典像素基线未定位到超过阈值的异常区域"

        elapsed_ms = (time.perf_counter() - started) * 1000
        return InferenceResult(
            prediction=prediction,
            confidence=round(confidence, 6),
            action=action,
            reason=reason,
            latency_ms=round(elapsed_ms, 3),
            model_name=self.name,
            node_id=node_id,
            model_version=self.version,
            preprocess_version=self.preprocess_version,
            detections=detections,
            image_quality=quality,
            anomaly_score=round(anomaly_score, 6),
            scene_complexity=round(scene_complexity, 6),
        )
