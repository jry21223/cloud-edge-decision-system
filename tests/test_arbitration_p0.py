from datetime import UTC, datetime, timedelta

import pytest

from common.schemas import ArbitrationRequest, EdgeProposal, InferenceResult, Route, TaskRequest
from services.controller import arbitration as arbitration_module
from services.controller import main as controller_main
from services.controller.fusion_store import FusionStore


def _proposal(
    node_id: str,
    *,
    prediction: str,
    action: str,
    confidence: float,
    observed_at: datetime,
    proposal_id: str | None = None,
    result_node_id: str | None = None,
) -> EdgeProposal:
    return EdgeProposal(
        proposal_id=proposal_id or f"proposal-{node_id}",
        node_id=node_id,
        result=InferenceResult(
            prediction=prediction,
            confidence=confidence,
            action=action,
            reason="test evidence",
            latency_ms=5,
            model_name="test-model",
            node_id=result_node_id or node_id,
        ),
        observed_at=observed_at,
        node_reliability=1.0,
        spatial_consistency=1.0,
    )


def _request(proposals: list[EdgeProposal], *, half_life_seconds: float = 5.0):
    return ArbitrationRequest(
        association_id="association-p0",
        task=TaskRequest(
            task_id="task-p0",
            trace_id="trace-p0",
            scene="industrial",
            payload={},
            metadata={"freshness_half_life_s": half_life_seconds},
        ),
        proposals=proposals,
    )


def _freeze_now(monkeypatch, instant: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr(arbitration_module, "datetime", FrozenDateTime)


def test_arbitration_rejects_proposal_and_result_node_identity_mismatch():
    now = datetime.now(UTC)
    request = _request(
        [
            _proposal(
                "edge-a",
                prediction="normal",
                action="continue",
                confidence=0.9,
                observed_at=now,
                result_node_id="spoofed-edge",
            ),
            _proposal(
                "edge-b",
                prediction="normal",
                action="continue",
                confidence=0.9,
                observed_at=now,
            ),
        ]
    )

    with pytest.raises(ValueError, match="node_id must match"):
        arbitration_module.arbitrate(request)


def test_duplicate_node_keeps_latest_proposal_without_inflating_evidence(monkeypatch):
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, now)
    stale_duplicate = _proposal(
        "edge-a",
        prediction="critical",
        action="shutdown",
        confidence=0.99,
        observed_at=now - timedelta(seconds=1),
        proposal_id="edge-a-old",
    )
    latest = _proposal(
        "edge-a",
        prediction="normal",
        action="continue",
        confidence=0.8,
        observed_at=now,
        proposal_id="edge-a-new",
    )
    peer = _proposal(
        "edge-b",
        prediction="normal",
        action="continue",
        confidence=0.8,
        observed_at=now,
        proposal_id="edge-b-only",
    )

    with_duplicate = arbitration_module.arbitrate(
        _request([stale_duplicate, latest, peer])
    )
    baseline = arbitration_module.arbitrate(_request([latest, peer]))

    assert with_duplicate.duplicate_proposals == 1
    assert with_duplicate.conflict is False
    assert with_duplicate.final_prediction == "normal"
    assert with_duplicate.evidence_scores == baseline.evidence_scores
    assert with_duplicate.consensus_score == baseline.consensus_score


def test_entire_stale_batch_is_absolutely_downweighted(monkeypatch):
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, now)
    stale_time = now - timedelta(seconds=10)
    stale_request = _request(
        [
            _proposal(
                "edge-a",
                prediction="warning",
                action="inspect",
                confidence=0.9,
                observed_at=stale_time,
            ),
            _proposal(
                "edge-b",
                prediction="warning",
                action="inspect",
                confidence=0.9,
                observed_at=stale_time,
            ),
        ],
        half_life_seconds=1.0,
    )
    fresh_request = stale_request.model_copy(
        update={
            "proposals": [
                proposal.model_copy(update={"observed_at": now})
                for proposal in stale_request.proposals
            ]
        }
    )

    stale_weights = arbitration_module._proposal_weights(stale_request)
    fresh_weights = arbitration_module._proposal_weights(fresh_request)

    assert max(stale_weights.values()) < 0.001
    assert max(stale_weights.values()) < max(fresh_weights.values()) / 500
    result = arbitration_module.arbitrate(stale_request)
    assert result.route == Route.CLOUD
    assert result.requires_cloud_review is True
    assert result.resolution_success is False
    assert result.conflict is False


@pytest.mark.asyncio
async def test_cloud_review_recommendation_is_not_persisted_as_autonomous_terminal(
    monkeypatch,
):
    now = datetime.now(UTC)
    request = _request(
        [
            _proposal(
                "edge-a",
                prediction="normal",
                action="continue",
                confidence=0.8,
                observed_at=now,
            ),
            _proposal(
                "edge-b",
                prediction="warning",
                action="inspect",
                confidence=0.8,
                observed_at=now,
            ),
        ]
    )

    store = FusionStore(":memory:")

    async def ignore_event(**_kwargs):
        return None

    monkeypatch.setattr(controller_main, "fusion_store", lambda: store)
    monkeypatch.setattr(controller_main, "record_event", ignore_event)

    result = await controller_main.arbitrate_edges(request)

    assert result.requires_cloud_review is True
    assert result.route == Route.CLOUD
    assert result.resolution_success is False
    assert store.get(request.association_id or "") is None
    store.close()
