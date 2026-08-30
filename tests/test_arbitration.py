from datetime import UTC, datetime, timedelta

from common.schemas import ArbitrationRequest, EdgeProposal, InferenceResult, Route, TaskRequest
from services.controller.arbitration import arbitrate


def proposal(
    node_id: str,
    *,
    prediction: str,
    action: str,
    confidence: float,
    observed_at: datetime,
    policy_version: str = "v1",
    node_reliability: float = 0.85,
    spatial_consistency: float = 1.0,
) -> EdgeProposal:
    return EdgeProposal(
        node_id=node_id,
        result=InferenceResult(
            prediction=prediction,
            action=action,
            confidence=confidence,
            reason="test",
            latency_ms=5,
            model_name="test-model",
            node_id=node_id,
        ),
        observed_at=observed_at,
        policy_version=policy_version,
        node_reliability=node_reliability,
        spatial_consistency=spatial_consistency,
    )


def test_arbitration_gives_emergency_observation_safety_priority():
    now = datetime.now(UTC)
    request = ArbitrationRequest(
        task=TaskRequest(scene="industrial", payload={}),
        proposals=[
            proposal("edge-a", prediction="normal", action="continue", confidence=0.98, observed_at=now),
            proposal(
                "edge-b",
                prediction="critical",
                action="inspect",
                confidence=0.61,
                observed_at=now - timedelta(seconds=1),
            ),
        ],
    )

    result = arbitrate(request)

    assert result.route == Route.EDGE_SAFETY
    assert result.chosen_node == "edge-b"
    assert result.final_action == "shutdown"
    assert result.conflict is True


def test_arbitration_uses_confidence_then_freshness():
    now = datetime.now(UTC)
    request = ArbitrationRequest(
        task=TaskRequest(scene="traffic", payload={}),
        proposals=[
            proposal(
                "edge-a",
                prediction="congested",
                action="divert",
                confidence=0.81,
                observed_at=now - timedelta(seconds=5),
                policy_version="v2",
            ),
            proposal(
                "edge-b",
                prediction="congested",
                action="divert",
                confidence=0.81,
                observed_at=now,
                policy_version="v1",
            ),
        ],
    )

    result = arbitrate(request)

    assert result.route == Route.PEER_EDGE
    assert result.chosen_node == "edge-b"
    assert result.conflict is False
    assert result.resolution_success is True


def test_arbitration_exact_tie_is_deterministic():
    now = datetime.now(UTC)
    request = ArbitrationRequest(
        task=TaskRequest(scene="industrial", payload={}),
        proposals=[
            proposal("edge-z", prediction="warning", action="inspect", confidence=0.8, observed_at=now),
            proposal("edge-a", prediction="warning", action="inspect", confidence=0.8, observed_at=now),
        ],
    )

    assert arbitrate(request).chosen_node == "edge-a"


def test_arbitration_uses_node_reliability_instead_of_raw_confidence_only():
    now = datetime.now(UTC)
    request = ArbitrationRequest(
        task=TaskRequest(scene="industrial", payload={}),
        proposals=[
            proposal(
                "edge-a",
                prediction="warning",
                action="inspect",
                confidence=0.80,
                observed_at=now,
                node_reliability=0.99,
            ),
            proposal(
                "edge-b",
                prediction="normal",
                action="continue",
                confidence=0.90,
                observed_at=now,
                node_reliability=0.30,
            ),
            proposal(
                "edge-c",
                prediction="normal",
                action="continue",
                confidence=0.60,
                observed_at=now,
                node_reliability=0.30,
            ),
        ],
    )

    result = arbitrate(request)

    assert result.chosen_node == "edge-a"
    assert result.final_prediction == "warning"
    assert result.resolution_success is True
    assert result.consensus_score >= 0.60


def test_balanced_conflict_requests_cloud_review():
    now = datetime.now(UTC)
    request = ArbitrationRequest(
        task=TaskRequest(scene="traffic", payload={}),
        proposals=[
            proposal(
                "edge-a",
                prediction="normal",
                action="keep_open",
                confidence=0.80,
                observed_at=now,
            ),
            proposal(
                "edge-b",
                prediction="congested",
                action="divert",
                confidence=0.80,
                observed_at=now,
            ),
        ],
    )

    result = arbitrate(request)

    assert result.route == Route.CLOUD
    assert result.resolution_success is False
    assert result.requires_cloud_review is True
    assert result.final_action == "review"


def test_fresh_observation_can_outweigh_stale_overconfident_observation():
    now = datetime.now(UTC)
    request = ArbitrationRequest(
        task=TaskRequest(scene="traffic", payload={}),
        proposals=[
            proposal(
                "edge-old",
                prediction="normal",
                action="keep_open",
                confidence=0.99,
                observed_at=now - timedelta(seconds=20),
            ),
            proposal(
                "edge-new",
                prediction="congested",
                action="divert",
                confidence=0.75,
                observed_at=now,
            ),
        ],
    )

    result = arbitrate(request)

    assert result.chosen_node == "edge-new"
    assert result.final_prediction == "congested"
