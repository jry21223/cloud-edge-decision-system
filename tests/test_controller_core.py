from common.schemas import InferenceResult, TaskRequest
from services.controller.core import conservative_fallback


def edge_result(prediction: str) -> InferenceResult:
    return InferenceResult(
        prediction=prediction,
        confidence=0.55,
        action="continue",
        reason="uncertain",
        latency_ms=2,
        model_name="mock",
        node_id="edge-a",
    )


def test_industrial_fallback_is_conservative():
    task = TaskRequest(scene="industrial", payload={})
    prediction, action, confidence, _ = conservative_fallback(task, edge_result("normal"))
    assert prediction == "warning"
    assert action == "inspect"
    assert confidence >= 0.6


def test_traffic_fallback_slows_traffic():
    task = TaskRequest(scene="traffic", payload={})
    prediction, action, _, _ = conservative_fallback(task, edge_result("normal"))
    assert prediction == "congested"
    assert action == "slow_traffic"
