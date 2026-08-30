from common.schemas import Route, TaskRequest
from services.edge_node.core import choose_local_route, infer_locally
from services.edge_node.llm_adapter import LlmDecision


def test_high_confidence_routes_to_edge(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 30, "vibration": 1, "current": 5},
        risk_level="low",
        metadata={"force_confidence": 0.95},
    )
    result = infer_locally(task, "edge-a")
    assert choose_local_route(task, result, 0.8) == Route.EDGE


def test_test_control_is_ignored_when_disabled(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "false")
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 74, "vibration": 4.9, "current": 13.8},
        metadata={"force_confidence": 0.95},
    )

    result = infer_locally(task, "edge-a")

    assert result.confidence < 0.8
    assert "测试控制" not in result.reason


def test_test_control_is_identified_when_enabled(monkeypatch, caplog):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    task = TaskRequest(
        task_id="test-control-enabled",
        scene="industrial",
        payload={"temperature": 74, "vibration": 4.9, "current": 13.8},
        metadata={"force_confidence": 0.95},
    )

    result = infer_locally(task, "edge-a")

    assert result.confidence == 0.95
    assert "测试控制 force_confidence 已应用" in result.reason
    assert "task_id=test-control-enabled control=force_confidence" in caplog.text


def test_low_confidence_requires_escalation(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 74, "vibration": 4.9, "current": 13.8},
        metadata={"force_confidence": 0.55},
    )
    result = infer_locally(task, "edge-a")
    assert choose_local_route(task, result, 0.8) is None


def test_critical_risk_never_waits_for_cloud(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 65, "vibration": 2, "current": 8},
        risk_level="critical",
        metadata={"force_confidence": 0.30},
    )
    result = infer_locally(task, "edge-a")
    assert choose_local_route(task, result, 0.8) == Route.EDGE_SAFETY


def test_critical_risk_skips_optional_llm(monkeypatch):
    def unexpected_llm_call(task):
        raise AssertionError("LLM must not delay a task explicitly marked critical")

    monkeypatch.setattr("services.edge_node.core.maybe_llm_inference", unexpected_llm_call)
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 30, "vibration": 1, "current": 5},
        risk_level="critical",
    )

    result = infer_locally(task, "edge-a")

    assert result.prediction == "normal"
    assert choose_local_route(task, result, 0.8) == Route.EDGE_SAFETY


def test_llm_refines_noncritical_decision_with_rule_confidence_guard(monkeypatch):
    monkeypatch.setattr(
        "services.edge_node.core.maybe_llm_inference",
        lambda task: LlmDecision(
            prediction="warning",
            action="inspect",
            confidence=0.72,
            reason="bearing vibration is elevated",
            model_name="test-llm",
        ),
    )
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 30, "vibration": 1, "current": 5},
    )

    result = infer_locally(task, "edge-a")

    assert result.prediction == "warning"
    assert result.action == "inspect"
    assert result.confidence == 0.72
    assert result.model_name == "test-llm+rule-confidence-guard"


def test_rule_emergency_does_not_call_llm(monkeypatch):
    def unexpected_llm_call(task):
        raise AssertionError("LLM must not run before a rule-triggered safety action")

    monkeypatch.setattr("services.edge_node.core.maybe_llm_inference", unexpected_llm_call)
    task = TaskRequest(
        scene="industrial",
        payload={"temperature": 95, "vibration": 1, "current": 5},
    )

    result = infer_locally(task, "edge-a")

    assert result.prediction == "critical"
    assert result.action == "shutdown"


def test_llm_emergency_label_forces_local_safety_action(monkeypatch):
    monkeypatch.setattr(
        "services.edge_node.core.maybe_llm_inference",
        lambda task: LlmDecision(
            prediction="critical",
            action="inspect",
            confidence=0.82,
            reason="temperature trend is unsafe",
            model_name="test-llm",
        ),
    )
    task = TaskRequest(
        scene="industrial",
        payload={"temperature":84, "vibration":7.2, "current":16.2},
    )

    result = infer_locally(task, "edge-a")

    assert result.prediction == "critical"
    assert result.action == "shutdown"
