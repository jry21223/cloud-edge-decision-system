from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common.schemas import NodeHeartbeat, NodeStatus


class NodeRegistry:
    """In-memory control-plane registry; suitable for one controller instance."""

    def __init__(self, *, ttl_seconds: int = 15) -> None:
        self.ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[str, tuple[NodeHeartbeat, datetime]] = {}

    def heartbeat(self, payload: NodeHeartbeat, *, now: datetime | None = None) -> NodeStatus:
        received_at = now or datetime.now(UTC)
        self._entries[payload.node_id] = (payload, received_at)
        return self._status(payload, received_at, now=received_at)

    def list_nodes(self, *, now: datetime | None = None) -> list[NodeStatus]:
        current_time = now or datetime.now(UTC)
        return [
            self._status(payload, last_seen, now=current_time)
            for _, (payload, last_seen) in sorted(self._entries.items())
        ]

    def select_peer(
        self,
        *,
        scene: str,
        excluded_node_ids: set[str] | None = None,
        now: datetime | None = None,
    ) -> NodeStatus | None:
        excluded = excluded_node_ids or set()
        candidates = [
            status
            for status in self.list_nodes(now=now)
            if (
                status.healthy
                and status.endpoint_url
                and status.node_id not in excluded
                and scene in status.supported_scenes
            )
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda status: (
                status.estimated_latency_ms
                + status.queue_depth * 10
                + status.load * 100
                + (1.0 - status.reliability) * 120,
                status.node_id,
            ),
        )

    def _status(self, payload: NodeHeartbeat, last_seen: datetime, *, now: datetime) -> NodeStatus:
        return NodeStatus(
            **payload.model_dump(),
            last_seen=last_seen,
            healthy=now - last_seen <= self.ttl,
        )
