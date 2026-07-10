from __future__ import annotations

import math
import os
import time
from typing import Any

from common.schemas import InferenceResult, Route, TaskRequest


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _boundary_confidence(values: list[tuple[float, float]]) -> float:
    """Estimate confidence from distance to known decision boundaries."""
    distances = [abs(value - boundary) / max(abs(boundary), 1.0) for value, boundary in values]
    closest = min(distances) if distances else 0.5
    return round(_clamp(0.55 + closest * 1.8, 0.55, 0.97), 4)


def _industrial_inference(payload: dict[str, Any]) -> tuple[str, str, str, float]:
    temperature = float(payload.get("temperature", 25.0))
    vibration = float(payload.get("vibration", 1.0))
    current = float(payload.get("current", 5.0))
    log_text = str(payload.get("log", "")).lower()

    critical_keywords = ("smoke", "explosion", "fire", "冒烟", "爆炸", "起火")
    warning_keywords = ("noise", "overheat", "异响", "过热", "抖动")

    critical = (
        temperature >= 90
        or vibration >= 8
        or current >= 18
        or any(word in log_text for word in critical_keywords)
    )
    warning = (
        temperature >= 75
        or vibration >= 5
        or current >= 14
        or any(word in log_text for word in warning_keywords)
    )

    confidence = _boundary_confidence(
        [
            (temperature, 75),
            (temperature, 90),
            (vibration, 5),
            (vibration, 8),
            (current, 14),
            (current, 18),
        ]
    )

    if critical:
        return "critical", "shutdown", "边缘规则检测到至少一个高危信号", confidence
    if warning:
        return "warning", "inspect", "边缘规则检测到一个或多个预警信号", confidence
    return "normal", "continue", "关键指标均处于本地安全范围", confidence


def _traffic_inference(payload: dict[str, Any]) -> tuple[str, str, str, float]:
    density = float(payload.get("vehicle_density", 0.2))
    speed = float(payload.get("average_speed", 45.0))
    queue = float(payload.get("queue_length", 5.0))
    accident = bool(payload.get("accident_reported", False))

    confidence = _boundary_confidence(
        [
            (density, 0.7),
            (density, 0.9),
            (speed, 20),
            (speed, 5),
            (queue, 20),
            (queue, 50),
        ]
    )

    if accident or (speed <= 5 and queue >= 50):
        return "incident", "close_lane", "检测到事故报告或严重停滞", confidence
    if density >= 0.7 or speed <= 20 or queue >= 20:
        return "congested", "divert", "交通指标达到拥堵预警条件", confidence
    return "normal", "keep_open", "路段运行指标处于正常范围", confidence


def infer_locally(task: TaskRequest, node_id: str) -> InferenceResult:
    started = time.perf_counter()
    if task.scene == "traffic":
        prediction, action, reason, confidence = _traffic_inference(task.payload)
    else:
        prediction, action, reason, confidence = _industrial_inference(task.payload)

    allow_test_controls = os.getenv("ALLOW_TEST_CONTROLS", "false").lower() == "true"
    if allow_test_controls and "force_confidence" in task.metadata:
        confidence = _clamp(float(task.metadata["force_confidence"]), 0.0, 1.0)
        reason = f"{reason}；测试模式覆盖 confidence"

    elapsed_ms = (time.perf_counter() - started) * 1000
    return InferenceResult(
        prediction=prediction,
        confidence=round(confidence, 4),
        action=action,
        reason=reason,
        latency_ms=round(elapsed_ms, 3),
        model_name="edge-rule-model-v0.1",
        node_id=node_id,
    )


def choose_local_route(task: TaskRequest, result: InferenceResult, threshold: float) -> Route | None:
    if task.risk_level == "critical" or result.prediction in {"critical", "incident"}:
        return Route.EDGE_SAFETY
    if result.confidence >= threshold:
        return Route.EDGE
    return None


def safety_action_for(scene: str) -> str:
    return "close_lane" if scene == "traffic" else "shutdown"
