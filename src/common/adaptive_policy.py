from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.schemas import Route, TaskRequest


@dataclass(frozen=True)
class NetworkSnapshot:
    """Normalized network telemetry used by the DREAM-CE policy."""

    availability: float = 1.0
    rtt_ms: float = 20.0
    jitter_ms: float = 0.0
    packet_loss: float = 0.0


@dataclass(frozen=True)
class ExecutionCandidate:
    route: Route
    predicted_latency_ms: float
    expected_accuracy: float
    availability: float = 1.0
    load: float = 0.0
    communication_kb: float = 0.0
    node_id: str | None = None


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: ExecutionCandidate
    score: float
    deadline_feasible: bool
    explanation: str


_RISK_WEIGHT = {"low": 0.8, "medium": 1.2, "high": 2.0, "critical": 3.0}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def network_snapshot(metadata: dict[str, Any]) -> NetworkSnapshot:
    raw = metadata.get("network", {})
    if not isinstance(raw, dict):
        raw = {}
    return NetworkSnapshot(
        availability=_clamp(float(raw.get("availability", 1.0))),
        rtt_ms=max(0.0, float(raw.get("rtt_ms", 20.0))),
        jitter_ms=max(0.0, float(raw.get("jitter_ms", 0.0))),
        packet_loss=_clamp(float(raw.get("packet_loss", 0.0))),
    )


def network_degradation(snapshot: NetworkSnapshot) -> float:
    latency_term = _clamp((snapshot.rtt_ms + snapshot.jitter_ms) / 500.0)
    return _clamp(
        0.45 * (1.0 - snapshot.availability)
        + 0.35 * snapshot.packet_loss
        + 0.20 * latency_term
    )


def dynamic_local_threshold(
    task: TaskRequest,
    *,
    base_threshold: float,
    elapsed_ms: float = 0.0,
) -> float:
    """Adapt the local-exit threshold without weakening emergency handling.

    A poor network or an expiring deadline lowers the threshold so basic service
    remains available. High-risk tasks raise it and therefore request stronger
    evidence unless the safety guard has already fired.
    """

    risk_adjustment = {"low": -0.04, "medium": 0.04, "high": 0.10, "critical": 0.15}
    degradation = network_degradation(network_snapshot(task.metadata))
    deadline_pressure = _clamp(elapsed_ms / max(float(task.deadline_ms), 1.0))
    threshold = (
        base_threshold
        + risk_adjustment[task.risk_level]
        - 0.18 * degradation
        - 0.08 * deadline_pressure
    )
    return round(_clamp(threshold, 0.55, 0.95), 4)


def rank_execution_candidates(
    task: TaskRequest,
    candidates: list[ExecutionCandidate],
    *,
    remaining_deadline_ms: float,
) -> list[ScoredCandidate]:
    """Rank routes with the DREAM-CE constrained cost function.

    Infeasible and unavailable remote routes are retained at the end for
    observability, but the caller should attempt feasible routes first.
    """

    risk_weight = _RISK_WEIGHT[task.risk_level]
    deadline = max(remaining_deadline_ms, 1.0)
    scored: list[ScoredCandidate] = []

    for item in candidates:
        latency_ratio = item.predicted_latency_ms / deadline
        deadline_feasible = item.predicted_latency_ms <= remaining_deadline_ms
        remote = item.route in {Route.PEER_EDGE, Route.CLOUD}
        available = item.availability >= 0.20

        latency_cost = min(latency_ratio, 3.0)
        error_cost = 2.0 * risk_weight * (1.0 - _clamp(item.expected_accuracy))
        availability_cost = 2.5 * (1.0 - _clamp(item.availability)) if remote else 0.0
        load_cost = 0.25 * _clamp(item.load)
        communication_cost = 0.05 * min(item.communication_kb / 1024.0, 4.0)
        fallback_cost = (0.45 + 0.25 * risk_weight) if item.route == Route.EDGE_FALLBACK else 0.0
        hard_penalty = 8.0 if (not deadline_feasible or (remote and not available)) else 0.0
        total = (
            latency_cost
            + error_cost
            + availability_cost
            + load_cost
            + communication_cost
            + fallback_cost
            + hard_penalty
        )
        scored.append(
            ScoredCandidate(
                candidate=item,
                score=round(total, 6),
                deadline_feasible=deadline_feasible and (available or not remote),
                explanation=(
                    f"latency={item.predicted_latency_ms:.1f}ms, "
                    f"accuracy={item.expected_accuracy:.3f}, availability={item.availability:.3f}, "
                    f"load={item.load:.3f}, cost={total:.4f}"
                ),
            )
        )

    return sorted(
        scored,
        key=lambda item: (
            not item.deadline_feasible,
            item.score,
            item.candidate.route.value,
            item.candidate.node_id or "",
        ),
    )
