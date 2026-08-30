from scripts.benchmark_system import RequestResult, percentile, summarize_results


def test_percentile_uses_linear_interpolation_and_handles_empty_input():
    values = [10.0, 20.0, 30.0, 40.0]

    assert percentile(values, 0) == 10.0
    assert percentile(values, 50) == 25.0
    assert percentile(values, 95) == 38.5
    assert percentile(values, 99) == 39.7
    assert percentile([], 50) is None


def test_summary_reports_client_observed_performance_and_traffic():
    results = [
        RequestResult(
            index=0,
            success=True,
            latency_ms=100.0,
            route="EDGE",
            request_bytes=100,
            response_bytes=200,
            status_code=200,
        ),
        RequestResult(
            index=1,
            success=True,
            latency_ms=300.0,
            route="CLOUD",
            request_bytes=110,
            response_bytes=220,
            status_code=200,
        ),
        RequestResult(
            index=2,
            success=False,
            latency_ms=500.0,
            route=None,
            request_bytes=120,
            response_bytes=20,
            status_code=503,
            error="HTTP 503",
        ),
    ]

    summary = summarize_results(results, deadline_ms=250, elapsed_s=2.0)

    assert summary == {
        "total_requests": 3,
        "successful_requests": 2,
        "failed_requests": 1,
        "success_rate": 2 / 3,
        "deadline_met_requests": 1,
        "deadline_met_rate": 1 / 3,
        "route_distribution": {"CLOUD": 1, "EDGE": 1},
        "request_bytes_total": 330,
        "response_bytes_total": 440,
        "traffic_bytes_total": 770,
        "elapsed_s": 2.0,
        "throughput_rps": 1.5,
        "successful_throughput_rps": 1.0,
        "latency_ms": {
            "count": 3,
            "mean": 300.0,
            "p50": 300.0,
            "p95": 480.0,
            "p99": 496.0,
        },
    }


def test_summary_handles_an_empty_run_without_division_by_zero():
    summary = summarize_results([], deadline_ms=200, elapsed_s=0.0)

    assert summary["success_rate"] == 0.0
    assert summary["deadline_met_rate"] == 0.0
    assert summary["throughput_rps"] == 0.0
    assert summary["latency_ms"] == {
        "count": 0,
        "mean": None,
        "p50": None,
        "p95": None,
        "p99": None,
    }
