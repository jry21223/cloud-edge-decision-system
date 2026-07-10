from __future__ import annotations

from typing import Any


def infer_industrial(payload: dict[str, Any]) -> tuple[str, str, str]:
    temperature = float(payload.get("temperature", 25.0))
    vibration = float(payload.get("vibration", 1.0))
    current = float(payload.get("current", 5.0))
    text = str(payload.get("log", "")).lower()

    danger_votes = sum(
        [
            temperature >= 85,
            vibration >= 7,
            current >= 16,
            any(k in text for k in ("smoke", "fire", "冒烟", "起火", "剧烈")),
        ]
    )
    warning_votes = sum([temperature >= 72, vibration >= 4.5, current >= 13])

    if danger_votes >= 2:
        return "critical", "shutdown", "云端融合多个接近高危阈值的信号，判定为严重故障"
    if danger_votes == 1 or warning_votes >= 1:
        return "warning", "inspect", "云端综合分析后建议安排检查"
    return "normal", "continue", "云端未发现需要升级处置的异常"


def infer_traffic(payload: dict[str, Any]) -> tuple[str, str, str]:
    density = float(payload.get("vehicle_density", 0.2))
    speed = float(payload.get("average_speed", 45.0))
    queue = float(payload.get("queue_length", 5.0))
    accident = bool(payload.get("accident_reported", False))

    danger_votes = sum([density >= 0.85, speed <= 8, queue >= 40, accident])
    warning_votes = sum([density >= 0.65, speed <= 25, queue >= 15])
    if danger_votes >= 2 or accident:
        return "incident", "close_lane", "云端融合交通指标后判定为事故或严重阻塞"
    if danger_votes == 1 or warning_votes >= 1:
        return "congested", "divert", "云端建议分流并持续观察"
    return "normal", "keep_open", "云端判断路段运行正常"
