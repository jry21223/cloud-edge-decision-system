import pytest

from common.schemas import EscalationRequest, InferenceResult, NodeHeartbeat, TaskRequest
from services.controller import main as controller_main
from services.controller.node_registry import NodeRegistry


class MockResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "prediction": "warning",
            "action": "inspect",
            "confidence": 0.88,
            "reason": "peer assessment",
            "latency_ms": 12.0,
            "model_name": "peer-rule",
            "node_id": "edge-b",
        }


class MockClient:
    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        self.request_url = ""

    async def __aenter__(self) -> "MockClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, object]) -> MockResponse:
        self.request_url = url
        return MockResponse()


def escalation_request(*, hop_count: int = 0) -> EscalationRequest:
    task = TaskRequest(scene="industrial", payload={}, deadline_ms=800)
    edge_result = InferenceResult(
        prediction="warning",
        action="inspect",
        confidence=0.55,
        reason="edge uncertainty",
        latency_ms=10,
        model_name="edge-rule",
        node_id="edge-a",
    )
    return EscalationRequest(
        task=task,
        edge_result=edge_result,
        elapsed_ms=10,
        origin_node="edge-a",
        visited_nodes=["edge-a"],
        hop_count=hop_count,
    )


@pytest.mark.asyncio
async def test_try_peer_dispatches_to_healthy_candidate(monkeypatch):
    registry = NodeRegistry()
    registry.heartbeat(
        NodeHeartbeat(
            node_id="edge-b",
            endpoint_url="http://edge-b:8000",
            estimated_latency_ms=20,
        )
    )
    monkeypatch.setattr(controller_main, "node_registry", registry)
    monkeypatch.setattr(controller_main, "PEER_ENABLED", True)
    monkeypatch.setattr(controller_main.httpx, "AsyncClient", MockClient)

    result = await controller_main.try_peer(
        escalation_request(),
        started=controller_main.time.perf_counter(),
        remaining_deadline_ms=790,
    )

    assert result is not None
    assert result.route == "PEER_EDGE"
    assert result.peer_result is not None
    assert result.peer_result.node_id == "edge-b"


@pytest.mark.asyncio
async def test_try_peer_refuses_a_second_peer_hop():
    assert (
        await controller_main.try_peer(
            escalation_request(hop_count=1),
            started=controller_main.time.perf_counter(),
            remaining_deadline_ms=790,
        )
        is None
    )


@pytest.mark.asyncio
async def test_try_peer_rejects_a_response_that_arrives_after_deadline(monkeypatch):
    registry = NodeRegistry()
    registry.heartbeat(
        NodeHeartbeat(
            node_id="edge-b",
            endpoint_url="http://edge-b:8000",
            estimated_latency_ms=20,
        )
    )
    clock = {"now": 0.0}

    class LateClient(MockClient):
        async def post(self, url: str, *, json: dict[str, object]) -> MockResponse:
            clock["now"] = 0.8
            return await super().post(url, json=json)

    monkeypatch.setattr(controller_main, "node_registry", registry)
    monkeypatch.setattr(controller_main, "PEER_ENABLED", True)
    monkeypatch.setattr(controller_main.time, "perf_counter", lambda: clock["now"])
    monkeypatch.setattr(controller_main.httpx, "AsyncClient", LateClient)

    result = await controller_main.try_peer(
        escalation_request(),
        started=0.0,
        remaining_deadline_ms=790,
    )

    assert result is None
