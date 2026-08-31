import pytest
from fastapi import HTTPException

from common.schemas import EventCreate, GroundTruthCreate
from services.recorder.main import create_event, put_ground_truth, reset, summary


@pytest.mark.asyncio
async def test_ground_truth_is_attached_post_hoc_and_scores_the_final_action():
    await reset()
    association_id = "synthetic-association-ground-truth"
    await create_event(
        EventCreate(
            task_id="synthetic-task-ground-truth",
            component="controller",
            event_type="arbitration_pending",
            route="CLOUD",
            data={
                "association_id": association_id,
                "conflict": True,
                "resolution_success": False,
                "final_prediction": "warning",
                "final_action": "review",
            },
        )
    )
    truth = GroundTruthCreate(
        prediction="critical",
        action="shutdown",
        source="synthetic_test_fixture",
    )

    created = await put_ground_truth(association_id, truth)
    repeated = await put_ground_truth(association_id, truth)
    before_final = await summary()

    assert created["created"] is True
    assert repeated["created"] is False
    assert before_final["arbitration"]["resolution_success_rate"] == 0.0

    await create_event(
        EventCreate(
            task_id="synthetic-task-ground-truth",
            component="edge-a",
            event_type="decision",
            route="CLOUD",
            data={
                "association_id": association_id,
                "final_prediction": "critical",
                "final_action": "shutdown",
            },
        )
    )

    after_final = await summary()
    assert after_final["arbitration"]["conflict_count"] == 1
    assert after_final["arbitration"]["labeled_conflicts"] == 1
    assert after_final["arbitration"]["resolution_success_rate"] == 1.0


@pytest.mark.asyncio
async def test_ground_truth_id_rejects_different_content():
    await reset()
    association_id = "synthetic-association-ground-truth-conflict"
    await put_ground_truth(
        association_id,
        GroundTruthCreate(prediction="normal", action="continue", source="synthetic"),
    )

    with pytest.raises(HTTPException) as raised:
        await put_ground_truth(
            association_id,
            GroundTruthCreate(prediction="critical", action="shutdown", source="synthetic"),
        )

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_pending_prediction_alone_is_not_counted_as_a_correct_final_decision():
    await reset()
    association_id = "synthetic-pending-not-final"
    await create_event(
        EventCreate(
            task_id=association_id,
            component="controller",
            event_type="arbitration_pending",
            data={
                "association_id": association_id,
                "conflict": True,
                "resolution_success": False,
                "final_prediction": "warning",
                "final_action": "review",
            },
        )
    )
    await put_ground_truth(
        association_id, GroundTruthCreate(prediction="warning", source="synthetic")
    )
    assert (await summary())["arbitration"]["resolution_success_rate"] == 0.0
