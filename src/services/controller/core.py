from __future__ import annotations

from common.schemas import InferenceResult, TaskRequest


def conservative_fallback(task: TaskRequest, edge_result: InferenceResult) -> tuple[str, str, float, str]:
    """Return a safe, deterministic result when remote inference is unavailable."""
    if task.scene == "traffic":
        if edge_result.prediction == "normal":
            return "congested", "slow_traffic", 0.60, "云端不可用，交通场景采用保守限速策略"
        return edge_result.prediction, edge_result.action, max(0.60, edge_result.confidence), "云端不可用，保留边缘预警结果"

    if edge_result.prediction == "normal":
        return "warning", "inspect", 0.60, "云端不可用，将不确定的正常结果降级为人工检查"
    return edge_result.prediction, edge_result.action, max(0.60, edge_result.confidence), "云端不可用，保留边缘预警结果"
