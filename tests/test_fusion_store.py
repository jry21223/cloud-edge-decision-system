from datetime import UTC, datetime, timedelta

import pytest

from common.schemas import ArbitrationRequest, EdgeProposal, InferenceResult, TaskRequest
from services.controller.arbitration import arbitrate
from services.controller.fusion_store import FusionConflictError, FusionStore


def _proposal(node_id: str, *, prediction: str, action: str) -> EdgeProposal:
    return EdgeProposal(
        proposal_id=f"proposal-{node_id}-{prediction}",
        node_id=node_id,
        result=InferenceResult(
            prediction=prediction,
            confidence=0.85,
            action=action,
            reason="test evidence",
            latency_ms=5,
            model_name="test-model",
            node_id=node_id,
        ),
        observed_at=datetime.now(UTC),
    )


def _request(*, payload: dict[str, object] | None = None) -> ArbitrationRequest:
    return ArbitrationRequest(
        association_id="workpiece-001:inspection:42",
        task=TaskRequest(
            task_id="task-001",
            trace_id="trace-001",
            scene="industrial",
            payload=payload or {"sample": 1},
        ),
        proposals=[
            _proposal("edge-a", prediction="warning", action="inspect"),
            _proposal("edge-b", prediction="warning", action="inspect"),
        ],
    )


def test_sqlite_reopen_preserves_terminal_decision_and_late_evidence(tmp_path):
    database = tmp_path / "fusion.db"
    request = _request()
    store = FusionStore(database)

    terminal, created = store.resolve(request, arbitrate(request))
    assert created is True
    assert terminal.association_id == request.association_id
    assert terminal.late_proposals_ignored == 0

    cached, created = store.resolve(request, arbitrate(request))
    assert created is False
    assert cached.final_prediction == terminal.final_prediction
    assert cached.final_action == terminal.final_action
    assert cached.idempotent_replay is True
    assert cached.late_proposals_ignored == 0

    late_request = request.model_copy(
        update={
            "proposals": [
                proposal.model_copy(
                    update={
                        "proposal_id": f"{proposal.proposal_id}-late",
                        "observed_at": proposal.observed_at + timedelta(seconds=1),
                    }
                )
                for proposal in request.proposals
            ]
        }
    )
    late, created = store.resolve(late_request, arbitrate(late_request))
    assert created is False
    assert late.idempotent_replay is False
    assert late.late_proposals_ignored == len(request.proposals)
    store.close()

    reopened = FusionStore(database)
    try:
        persisted = reopened.get(request.association_id or "")
        assert persisted is not None
        assert persisted.final_prediction == terminal.final_prediction
        assert persisted.final_action == terminal.final_action
        assert persisted.late_proposals_ignored == len(request.proposals)
    finally:
        reopened.close()


def test_same_association_with_different_task_payload_is_an_idempotency_conflict(tmp_path):
    store = FusionStore(tmp_path / "fusion.db")
    original = _request(payload={"sample": 1})
    conflicting = original.model_copy(
        update={"task": original.task.model_copy(update={"payload": {"sample": 2}})}
    )

    try:
        store.resolve(original, arbitrate(original))

        with pytest.raises(FusionConflictError, match="different task payload"):
            store.resolve(conflicting, arbitrate(conflicting))
    finally:
        store.close()


def test_ground_truth_metadata_does_not_change_the_idempotency_hash(tmp_path):
    store = FusionStore(tmp_path / "fusion.db")
    original = _request()
    labeled = original.model_copy(
        update={
            "task": original.task.model_copy(
                update={"metadata": {"ground_truth_prediction": "warning"}}
            )
        }
    )

    try:
        terminal, created = store.resolve(original, arbitrate(original))
        assert created is True

        cached, created = store.resolve(labeled, arbitrate(labeled))
        assert created is False
        assert cached.final_prediction == terminal.final_prediction
    finally:
        store.close()


def test_pending_review_cannot_overwrite_an_existing_terminal(tmp_path):
    store = FusionStore(tmp_path / "fusion.db")
    request = _request()
    try:
        terminal, _ = store.resolve(request, arbitrate(request))
        pending = terminal.model_copy(update={"requires_cloud_review": True, "final_action": "review"})
        changed = request.model_copy(
            update={"proposals": [p.model_copy(update={"proposal_id": p.proposal_id + "-late"}) for p in request.proposals]}
        )
        replay, created = store.resolve(changed, pending)
        assert created is False
        assert replay.requires_cloud_review is False
        assert replay.final_action == terminal.final_action
        original_replay, _ = store.resolve(request, arbitrate(request))
        assert original_replay.idempotent_replay is True
        assert original_replay.late_proposals_ignored == len(request.proposals)
    finally:
        store.close()
