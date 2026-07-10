from common.schemas import Route, TaskRequest
from services.edge_node.core import choose_local_route, infer_locally


def test_high_confidence_routes_to_edge(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 30, "vibration": 1, "current": 5},
        risk_level="low",
        metadata={"force_confidence": 0.95},
    )
    result = infer_locally(task, "edge-a")
    assert choose_local_route(task, result, 0.8) == Route.EDGE


def test_low_confidence_requires_escalation(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 74, "vibration": 4.9, "current": 13.8},
        metadata={"force_confidence": 0.55},
    )
    result = infer_locally(task, "edge-a")
    assert choose_local_route(task, result, 0.8) is None


def test_critical_risk_never_waits_for_cloud(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 65, "vibration": 2, "current": 8},
        risk_level="critical",
        metadata={"force_confidence": 0.30},
    )
    result = infer_locally(task, "edge-a")
    assert choose_local_route(task, result, 0.8) == Route.EDGE_SAFETY
