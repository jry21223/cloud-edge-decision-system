import pytest
from scripts.evaluate_industrial_vision import evaluate_records


def test_evaluation_reports_visual_and_cloud_edge_metrics_from_one_frozen_run():
    summary = evaluate_records(
        [
            {
                "truth": "scratch",
                "prediction": "scratch",
                "action": "reject",
                "latency_ms": 100,
                "route": "EDGE",
                "uploaded_bytes": 0,
                "deadline_met": True,
            },
            {
                "truth": "crack",
                "prediction": "scratch",
                "action": "pass",
                "latency_ms": 200,
                "route": "CLOUD",
                "uploaded_bytes": 1000,
                "deadline_met": True,
            },
            {
                "truth": "normal",
                "prediction": "normal",
                "action": "pass",
                "latency_ms": 300,
                "route": "EDGE",
                "uploaded_bytes": 0,
                "deadline_met": False,
            },
        ]
    )

    assert summary["sample_count"] == 3
    assert summary["visual"]["accuracy"] == pytest.approx(2 / 3)
    assert summary["visual"]["per_class"]["scratch"] == {
        "precision": 0.5,
        "recall": 1.0,
        "f1": pytest.approx(2 / 3),
        "support": 1,
    }
    assert summary["visual"]["per_class"]["crack"] == {
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "support": 1,
    }
    assert summary["visual"]["serious_defect_miss_rate"] == 1.0
    assert summary["system"] == {
        "mean_latency_ms": 200.0,
        "p95_latency_ms": 290.0,
        "deadline_met_rate": pytest.approx(2 / 3),
        "cloud_route_rate": pytest.approx(1 / 3),
        "uploaded_bytes_total": 1000,
        "uploaded_bytes_mean": pytest.approx(1000 / 3),
    }


def test_evaluation_rejects_unknown_ground_truth_labels():
    with pytest.raises(ValueError, match="unsupported truth label"):
        evaluate_records(
            [
                {
                    "truth": "rust",
                    "prediction": "normal",
                    "action": "pass",
                    "latency_ms": 1,
                    "route": "EDGE",
                    "uploaded_bytes": 0,
                    "deadline_met": True,
                }
            ]
        )


def test_shutdown_is_a_safe_detection_not_a_serious_defect_miss():
    summary = evaluate_records(
        [
            {
                "truth": "crack",
                "prediction": "crack",
                "action": "shutdown",
                "latency_ms": 5,
                "route": "EDGE_SAFETY",
                "uploaded_bytes": 0,
                "deadline_met": True,
            }
        ]
    )

    assert summary["visual"]["serious_defect_miss_rate"] == 0.0
