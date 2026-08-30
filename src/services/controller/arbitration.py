from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime

from common.schemas import ArbitrationRequest, ArbitrationResponse, EdgeProposal, Route

_EMERGENCY_PREDICTIONS = {"critical", "incident"}


def _policy_version_key(version: str) -> tuple[int, ...]:
    numbers = tuple(int(value) for value in re.findall(r"\d+", version))
    return numbers or (0,)


def _calibrated_confidence(confidence: float, temperature: float) -> float:
    """Apply binary temperature scaling when only a confidence is available."""

    clipped = max(1e-6, min(1.0 - 1e-6, confidence))
    logit = math.log(clipped / (1.0 - clipped))
    return 1.0 / (1.0 + math.exp(-logit / max(temperature, 1e-3)))


def _proposal_weights(request: ArbitrationRequest) -> dict[str, float]:
    proposals = request.proposals
    newest: datetime = max(item.observed_at for item in proposals)
    newest_version = max(_policy_version_key(item.policy_version) for item in proposals)
    half_life_seconds = max(float(request.task.metadata.get("freshness_half_life_s", 5.0)), 0.1)
    temperature = max(float(request.task.metadata.get("confidence_temperature", 1.0)), 0.05)

    weights: dict[str, float] = {}
    for item in proposals:
        age_seconds = max(0.0, (newest - item.observed_at).total_seconds())
        freshness = math.exp(-math.log(2.0) * age_seconds / half_life_seconds)
        version_factor = 1.0 if _policy_version_key(item.policy_version) == newest_version else 0.95
        confidence = _calibrated_confidence(item.result.confidence, temperature)
        # Keep a small topology floor so a missing calibration does not erase evidence.
        topology_factor = 0.5 + 0.5 * item.spatial_consistency
        weights[item.node_id] = (
            confidence
            * item.node_reliability
            * freshness
            * topology_factor
            * version_factor
        )
    return weights


def _winner(items: list[EdgeProposal], weights: dict[str, float]) -> EdgeProposal:
    return min(
        items,
        key=lambda item: (
            -weights[item.node_id],
            -item.observed_at.timestamp(),
            tuple(-number for number in _policy_version_key(item.policy_version)),
            item.node_id,
        ),
    )


def _safety_action(scene: str) -> str:
    return "close_lane" if scene == "traffic" else "shutdown"


def arbitrate(request: ArbitrationRequest) -> ArbitrationResponse:
    """DREAM-Fuse: reliability-, freshness-, and topology-aware evidence fusion."""

    proposals = request.proposals
    weights = _proposal_weights(request)
    grouped: dict[tuple[str, str], list[EdgeProposal]] = defaultdict(list)
    for item in proposals:
        grouped[(item.result.prediction, item.result.action)].append(item)

    raw_evidence = {
        outcome: sum(weights[item.node_id] for item in items)
        for outcome, items in grouped.items()
    }
    total_evidence = max(sum(raw_evidence.values()), 1e-9)
    normalized = {
        outcome: evidence / total_evidence for outcome, evidence in raw_evidence.items()
    }
    conflict = len(grouped) > 1

    emergency_items = [
        item for item in proposals if item.result.prediction in _EMERGENCY_PREDICTIONS
    ]
    emergency_supported = bool(emergency_items) and (
        request.task.risk_level in {"high", "critical"}
        or len(emergency_items) >= 2
        or max(weights[item.node_id] for item in emergency_items) >= 0.40
    )

    requires_cloud_review = False
    if emergency_supported:
        chosen = _winner(emergency_items, weights)
        chosen_outcome = (chosen.result.prediction, chosen.result.action)
        route = Route.EDGE_SAFETY
        action = _safety_action(request.task.scene)
        consensus = normalized[chosen_outcome]
        resolved = True
        reason = "DREAM-Fuse safety evidence triggered an immediate conservative action"
    else:
        chosen_outcome = min(
            normalized,
            key=lambda outcome: (-normalized[outcome], outcome[0], outcome[1]),
        )
        chosen = _winner(grouped[chosen_outcome], weights)
        consensus = normalized[chosen_outcome]
        minimum_consensus = float(request.task.metadata.get("minimum_consensus", 0.60))
        resolved = not conflict or consensus >= minimum_consensus
        requires_cloud_review = conflict and not resolved
        route = Route.CLOUD if requires_cloud_review else Route.PEER_EDGE
        action = "review" if requires_cloud_review else chosen.result.action
        reason = (
            "DREAM-Fuse evidence margin is insufficient; cloud review required"
            if requires_cloud_review
            else "DREAM-Fuse selected the strongest calibrated multi-edge evidence"
        )

    evidence_scores = {
        f"{prediction}|{action_name}": round(score, 6)
        for (prediction, action_name), score in sorted(normalized.items())
    }
    fused_confidence = min(1.0, 0.5 * chosen.result.confidence + 0.5 * consensus)

    return ArbitrationResponse(
        task_id=request.task.task_id,
        route=route,
        chosen_node=chosen.node_id,
        final_prediction=chosen.result.prediction,
        final_action=action,
        final_confidence=round(fused_confidence, 6),
        conflict=conflict,
        resolution_success=resolved,
        resolution_reason=reason,
        consensus_score=round(consensus, 6),
        requires_cloud_review=requires_cloud_review,
        evidence_scores=evidence_scores,
    )
