from common.adaptive_policy import (
    ExecutionCandidate,
    dynamic_local_threshold,
    network_snapshot,
    rank_execution_candidates,
)
from common.schemas import EscalationRequest, InferenceResult, NodeHeartbeat, Route, TaskRequest
from services.controller import main as controller_main
from services.controller.node_registry import NodeRegistry


def _escalation(task: TaskRequest) -> EscalationRequest:
    return EscalationRequest(
        task=task,
        edge_result=InferenceResult(
            prediction="normal",
            confidence=0.55,
            action="continue",
            reason="uncertain",
            latency_ms=10,
            model_name="edge-model",
            node_id="edge-a",
        ),
        elapsed_ms=10,
        origin_node="edge-a",
        visited_nodes=["edge-a"],
    )


def test_network_degradation_lowers_local_exit_threshold():
    healthy = TaskRequest(scene="industrial", payload={}, risk_level="medium")
    degraded = TaskRequest(
        scene="industrial",
        payload={},
        risk_level="medium",
        metadata={
            "network": {
                "availability": 0.2,
                "rtt_ms": 500,
                "jitter_ms": 80,
                "packet_loss": 0.10,
            }
        },
    )

    healthy_threshold = dynamic_local_threshold(healthy, base_threshold=0.80)
    degraded_threshold = dynamic_local_threshold(degraded, base_threshold=0.80)

    assert degraded_threshold < healthy_threshold
    assert degraded_threshold >= 0.55


def test_high_risk_raises_required_confidence():
    low = TaskRequest(scene="traffic", payload={}, risk_level="low")
    high = TaskRequest(scene="traffic", payload={}, risk_level="high")

    assert dynamic_local_threshold(high, base_threshold=0.80) > dynamic_local_threshold(
        low, base_threshold=0.80
    )


def test_route_planner_prefers_reliable_low_latency_peer():
    task = TaskRequest(scene="traffic", payload={}, risk_level="medium", deadline_ms=200)
    candidates = [
        ExecutionCandidate(Route.PEER_EDGE, 40, 0.95, node_id="edge-b"),
        ExecutionCandidate(Route.CLOUD, 100, 0.96, node_id="cloud"),
        ExecutionCandidate(Route.EDGE_FALLBACK, 1, 0.50, node_id="edge-a"),
    ]

    plan = rank_execution_candidates(task, candidates, remaining_deadline_ms=200)

    assert plan[0].candidate.route == Route.PEER_EDGE
    assert plan[0].deadline_feasible is True


def test_route_planner_rejects_unavailable_remote_routes():
    task = TaskRequest(scene="industrial", payload={}, deadline_ms=100)
    candidates = [
        ExecutionCandidate(Route.CLOUD, 80, 0.98, availability=0.0),
        ExecutionCandidate(Route.PEER_EDGE, 120, 0.90, availability=0.0),
        ExecutionCandidate(Route.EDGE_FALLBACK, 1, 0.55),
    ]

    plan = rank_execution_candidates(task, candidates, remaining_deadline_ms=100)

    assert plan[0].candidate.route == Route.EDGE_FALLBACK
    assert network_snapshot({"network": {"packet_loss": 0.1}}).packet_loss == 0.1


def test_controller_plan_uses_live_peer_reliability(monkeypatch):
    registry = NodeRegistry()
    registry.heartbeat(
        NodeHeartbeat(
            node_id="edge-b",
            endpoint_url="http://edge-b:8000",
            estimated_latency_ms=20,
            reliability=0.95,
        )
    )
    monkeypatch.setattr(controller_main, "node_registry", registry)
    request = _escalation(TaskRequest(scene="industrial", payload={}, deadline_ms=800))

    plan = controller_main.build_execution_plan(request, remaining_deadline_ms=790)

    assert plan[0].candidate.route == Route.PEER_EDGE
    assert plan[0].candidate.node_id == "edge-b"


def test_controller_plan_keeps_basic_service_during_outage(monkeypatch):
    monkeypatch.setattr(controller_main, "node_registry", NodeRegistry())
    request = _escalation(
        TaskRequest(
            scene="industrial",
            payload={},
            deadline_ms=800,
            metadata={"network": {"availability": 0.0, "packet_loss": 1.0}},
        )
    )

    plan = controller_main.build_execution_plan(request, remaining_deadline_ms=790)

    assert plan[0].candidate.route == Route.EDGE_FALLBACK
