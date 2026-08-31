from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from common.fallback import conservative_fallback
from common.schemas import EscalationRequest, InferenceResult, Route, TaskRequest
from services.controller import main as controller_main
from services.edge_node import main as edge_main


class UnavailableControllerClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, *args, **kwargs):
        raise httpx.ConnectError("controller unavailable")


@pytest.mark.parametrize("scene,expected", [("industrial", "shutdown"), ("traffic", "close_lane")])
def test_critical_risk_fallback_cannot_be_reduced_to_inspection(scene, expected):
    task = TaskRequest(scene=scene, payload={}, risk_level="critical")
    uncertain = InferenceResult(
        prediction="normal", action="continue", confidence=0.55,
        reason="uncertain fixture", latency_ms=1, model_name="fixture", node_id="edge-a",
    )
    assert conservative_fallback(task, uncertain)[1] == expected


@pytest.mark.asyncio
async def test_edge_falls_back_when_deadline_is_exhausted(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    recorder = AsyncMock()
    monkeypatch.setattr(edge_main, "record_event", recorder)

    response = await edge_main.process_task(
        TaskRequest(
            scene="industrial",
            payload={"temperature": 30, "vibration": 1, "current": 5},
            deadline_ms=50,
            metadata={"force_confidence": 0.55},
        )
    )

    assert response.route == Route.EDGE_FALLBACK
    assert response.degraded is True
    assert "不足远端调用的最小预算" in response.decision_reason
    recorder.assert_awaited_once()


@pytest.mark.asyncio
async def test_edge_falls_back_when_controller_is_unavailable(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    recorder = AsyncMock()
    monkeypatch.setattr(edge_main, "record_event", recorder)
    monkeypatch.setattr(edge_main.httpx, "AsyncClient", UnavailableControllerClient)

    response = await edge_main.process_task(
        TaskRequest(
            scene="industrial",
            payload={"temperature": 30, "vibration": 1, "current": 5},
            deadline_ms=800,
            metadata={"force_confidence": 0.55},
        )
    )

    assert response.route == Route.EDGE_FALLBACK
    assert response.degraded is True
    assert "Controller 不可用或调用超时" in response.decision_reason
    recorder.assert_awaited_once()


@pytest.mark.asyncio
async def test_controller_does_not_call_cloud_after_deadline_budget_is_spent(monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(controller_main, "record_event", recorder)

    edge_result = InferenceResult(
        prediction="normal",
        confidence=0.55,
        action="continue",
        reason="uncertain",
        latency_ms=2,
        model_name="mock",
        node_id="edge-a",
    )
    response = await controller_main.escalate(
        EscalationRequest(
            task=TaskRequest(scene="industrial", payload={}, deadline_ms=100),
            edge_result=edge_result,
            elapsed_ms=30,
        )
    )

    assert response.route == Route.EDGE_FALLBACK
    assert response.degraded is True
    assert "剩余 deadline 仅 70.0ms" in response.decision_reason
    recorder.assert_awaited_once()


@pytest.mark.asyncio
async def test_controller_rechecks_budget_after_peer_failure(monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(controller_main, "record_event", recorder)

    clock = {"now": 0.0}
    monkeypatch.setattr(controller_main.time, "perf_counter", lambda: clock["now"])

    plan = [
        SimpleNamespace(candidate=SimpleNamespace(route=Route.PEER_EDGE), explanation="peer"),
        SimpleNamespace(candidate=SimpleNamespace(route=Route.CLOUD), explanation="cloud"),
        SimpleNamespace(
            candidate=SimpleNamespace(route=Route.EDGE_FALLBACK),
            explanation="fallback",
        ),
    ]
    monkeypatch.setattr(controller_main, "build_execution_plan", lambda *_args, **_kwargs: plan)

    async def peer_consumes_budget(*_args, **_kwargs):
        clock["now"] = 0.86
        return None

    monkeypatch.setattr(controller_main, "try_peer", peer_consumes_budget)

    def cloud_must_not_start(*_args, **_kwargs):
        raise AssertionError("Cloud call started after the Peer consumed the deadline budget")

    monkeypatch.setattr(controller_main.httpx, "AsyncClient", cloud_must_not_start)

    edge_result = InferenceResult(
        prediction="warning",
        confidence=0.55,
        action="inspect",
        reason="uncertain",
        latency_ms=10,
        model_name="mock",
        node_id="edge-a",
    )
    response = await controller_main.escalate(
        EscalationRequest(
            task=TaskRequest(scene="industrial", payload={}, deadline_ms=1000),
            edge_result=edge_result,
            elapsed_ms=100,
            origin_node="edge-a",
            visited_nodes=["edge-a"],
        )
    )

    assert response.route == Route.EDGE_FALLBACK
    assert "前序路径消耗预算后仅剩 40.0ms" in response.decision_reason
    recorder.assert_awaited_once()
