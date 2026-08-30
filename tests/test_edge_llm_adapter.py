from services.edge_node.llm_adapter import _parse_label


def test_parse_label_after_reasoning_text():
    output = "<think>inspect the measurements first</think>\n风险等级：warning"

    result = _parse_label(output, "industrial", "test-llm")

    assert result is not None
    assert result.prediction == "warning"
    assert result.action == "inspect"
    assert result.confidence == 0.75


def test_parse_label_rejects_scene_invalid_label():
    output = "风险等级：critical"

    assert _parse_label(output, "traffic", "test-llm") is None
