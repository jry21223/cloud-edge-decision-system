from datetime import UTC, datetime, timedelta

from common.schemas import NodeHeartbeat
from services.controller.node_registry import NodeRegistry


def heartbeat(node_id: str, *, load: float, latency_ms: float) -> NodeHeartbeat:
    return NodeHeartbeat(
        node_id=node_id,
        endpoint_url=f"http://{node_id}:8000",
        load=load,
        queue_depth=1,
        estimated_latency_ms=latency_ms,
        model_version="rule-v0.1",
    )


def test_registry_marks_stale_nodes_unhealthy():
    registry = NodeRegistry(ttl_seconds=10)
    started = datetime(2026, 7, 12, tzinfo=UTC)
    registry.heartbeat(heartbeat("edge-a", load=0.1, latency_ms=20), now=started)

    nodes = registry.list_nodes(now=started + timedelta(seconds=11))

    assert nodes[0].healthy is False


def test_registry_selects_lowest_cost_healthy_peer():
    registry = NodeRegistry(ttl_seconds=10)
    started = datetime(2026, 7, 12, tzinfo=UTC)
    registry.heartbeat(heartbeat("edge-a", load=0.9, latency_ms=20), now=started)
    registry.heartbeat(heartbeat("edge-b", load=0.1, latency_ms=25), now=started)

    selected = registry.select_peer(
        scene="industrial",
        excluded_node_ids={"edge-a"},
        now=started + timedelta(seconds=1),
    )

    assert selected is not None
    assert selected.node_id == "edge-b"
