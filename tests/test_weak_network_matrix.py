from __future__ import annotations

import json
from argparse import Namespace
from copy import deepcopy

import pytest
from scripts.benchmark_system import BenchmarkConfig, RequestResult, summarize_results
from scripts.run_weak_network_matrix import (
    PROFILES,
    ToxiproxyController,
    measure_recovery,
    run_matrix,
)


class _JsonResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return deepcopy(self._payload)


def _benchmark_config() -> BenchmarkConfig:
    return BenchmarkConfig(
        url="http://test/v1/tasks",
        request_count=3,
        concurrency=1,
        scene="industrial",
        deadline_ms=200,
        timeout_ms=1000,
        fault_profile="test",
    )


def test_profile_contract_distinguishes_connection_failure_from_packet_loss() -> None:
    assert {"N0", "RTT100", "RTT300", "BW5M", "CF5", "OFFLINE"} <= set(PROFILES)

    rtt100 = PROFILES["RTT100"]
    assert {(toxic.stream, toxic.attributes["latency"]) for toxic in rtt100.toxics} == {
        ("upstream", 50),
        ("downstream", 50),
    }

    bandwidth = PROFILES["BW5M"]
    assert all(toxic.toxic_type == "bandwidth" for toxic in bandwidth.toxics)
    assert all(toxic.attributes == {"rate": 625} for toxic in bandwidth.toxics)

    connection_failure = PROFILES["CF5"]
    assert len(connection_failure.toxics) == 1
    assert connection_failure.toxics[0].toxic_type == "reset_peer"
    assert connection_failure.toxics[0].toxicity == 0.05
    assert "not packet loss" in connection_failure.interpretation
    assert all(toxic.toxic_type != "packet_loss" for profile in PROFILES.values() for toxic in profile.toxics)
    assert PROFILES["OFFLINE"].enabled is False


@pytest.mark.asyncio
async def test_toxiproxy_controller_replaces_existing_toxics_and_verifies_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ToxiproxyController("http://toxiproxy:8474")
    state = {"enabled": False, "toxics": [{"name": "old-toxic", "type": "timeout"}]}
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, suffix: str, **kwargs) -> _JsonResponse:
        calls.append((method, suffix, kwargs))
        if method == "GET":
            return _JsonResponse(state)
        if method == "DELETE":
            toxic_name = suffix.rsplit("/", 1)[-1]
            state["toxics"] = [item for item in state["toxics"] if item["name"] != toxic_name]
        elif suffix.endswith("/toxics"):
            state["toxics"].append(deepcopy(kwargs["json"]))
        else:
            state["enabled"] = bool(kwargs["json"]["enabled"])
        return _JsonResponse({})

    monkeypatch.setattr(controller, "_request", fake_request)

    applied = await controller.apply(PROFILES["RTT100"])
    assert applied["enabled"] is True
    assert {item["name"] for item in applied["toxics"]} == {"latency-up", "latency-down"}
    assert ("DELETE", "/proxies/cloud/toxics/old-toxic", {}) in calls

    offline = await controller.apply(PROFILES["OFFLINE"])
    assert offline["enabled"] is False
    assert offline["toxics"] == []


@pytest.mark.asyncio
async def test_controller_rejects_an_unverified_proxy_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ToxiproxyController("http://toxiproxy:8474")

    async def no_op(*_args, **_kwargs) -> None:
        return None

    async def wrong_state() -> dict:
        return {"enabled": False, "toxics": []}

    monkeypatch.setattr(controller, "clear_toxics", no_op)
    monkeypatch.setattr(controller, "set_enabled", no_op)
    monkeypatch.setattr(controller, "state", wrong_state)

    with pytest.raises(RuntimeError, match="does not match profile N0"):
        await controller.apply(PROFILES["N0"])


@pytest.mark.asyncio
async def test_recovery_requires_three_consecutive_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rates = iter((0.80, 0.96, 0.97, 0.99))

    async def fake_benchmark(_config: BenchmarkConfig) -> dict:
        rate = next(rates)
        return {
            "summary": {
                "business_retention": {
                    "eligible_tasks": 100,
                    "retained_tasks": round(rate * 100),
                    "rate": rate,
                }
            }
        }

    monkeypatch.setattr("scripts.run_weak_network_matrix.run_benchmark", fake_benchmark)

    result = await measure_recovery(
        config=_benchmark_config(),
        baseline_retention=1.0,
        consecutive_windows=3,
        timeout_s=1.0,
        window_seconds=0.0,
    )

    assert result["recovered"] is True
    assert result["threshold"] == 0.95
    assert [window["retention"] for window in result["windows"]] == [0.80, 0.96, 0.97, 0.99]


def test_retention_safety_and_false_isolation_keep_all_tasks_in_denominators() -> None:
    results = [
        RequestResult(
            index=0,
            success=True,
            latency_ms=100,
            route="EDGE",
            request_bytes=1,
            response_bytes=1,
            status_code=200,
            expected_prediction="normal",
            risk_level="low",
            final_prediction="normal",
            final_action="continue",
        ),
        RequestResult(
            index=1,
            success=True,
            latency_ms=100,
            route="EDGE",
            request_bytes=1,
            response_bytes=1,
            status_code=200,
            expected_prediction="critical",
            risk_level="critical",
            final_prediction="critical",
            final_action="continue",
        ),
        RequestResult(
            index=2,
            success=True,
            latency_ms=300,
            route="CLOUD",
            request_bytes=1,
            response_bytes=1,
            status_code=200,
            expected_prediction="warning",
            risk_level="medium",
            final_prediction="warning",
            final_action="inspect",
        ),
        RequestResult(
            index=3,
            success=False,
            latency_ms=1000,
            route=None,
            request_bytes=1,
            response_bytes=0,
            status_code=None,
            error="timeout",
            expected_prediction="incident",
            risk_level="critical",
        ),
        RequestResult(
            index=4,
            success=True,
            latency_ms=100,
            route="EDGE_FALLBACK",
            request_bytes=1,
            response_bytes=1,
            status_code=200,
            expected_prediction="normal",
            risk_level="low",
            final_prediction="warning",
            final_action="shutdown",
        ),
    ]

    summary = summarize_results(results, deadline_ms=200, elapsed_s=1.0)

    assert summary["business_retention"] == {
        "eligible_tasks": 5,
        "retained_tasks": 3,
        "rate": 0.6,
        "definition": "valid scene action returned within deadline / all injected tasks",
    }
    assert summary["safety"] == {
        "severe_tasks": 2,
        "severe_misses": 2,
        "severe_miss_rate": 1.0,
        "normal_tasks": 2,
        "false_isolations": 1,
        "false_isolation_rate": 0.5,
    }


@pytest.mark.asyncio
async def test_matrix_writes_profile_evidence_and_always_restores_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    controllers = []

    class FakeController:
        def __init__(self, _api_url: str) -> None:
            self.restore_calls = 0
            controllers.append(self)

        async def apply(self, profile) -> dict:
            return {
                "enabled": profile.enabled,
                "toxics": [
                    {
                        "name": toxic.name,
                        "type": toxic.toxic_type,
                        "toxicity": toxic.toxicity,
                    }
                    for toxic in profile.toxics
                ],
            }

        async def restore(self) -> dict:
            self.restore_calls += 1
            return {"enabled": True, "toxics": []}

    async def fake_benchmark(config: BenchmarkConfig) -> dict:
        rate = 1.0 if config.fault_profile == "N0" else 0.9
        return {
            "summary": {
                "total_requests": config.request_count,
                "cloud_path_attempted_requests": config.request_count,
                "business_retention": {"rate": rate},
            }
        }

    async def fake_recovery(**_kwargs) -> dict:
        return {"recovered": True, "recovery_time_ms": 10.0, "windows": []}

    monkeypatch.setattr("scripts.run_weak_network_matrix.ToxiproxyController", FakeController)
    monkeypatch.setattr("scripts.run_weak_network_matrix.run_benchmark", fake_benchmark)
    monkeypatch.setattr("scripts.run_weak_network_matrix.measure_recovery", fake_recovery)

    args = Namespace(
        output_dir=tmp_path,
        toxiproxy_api_url="http://toxiproxy:8474",
        profiles=["N0", "CF5"],
        url="http://edge-a/v1/tasks",
        requests=3,
        concurrency=1,
        scene="industrial",
        deadline_ms=200,
        timeout_ms=1000,
        recovery_window_requests=3,
        recovery_timeout_s=1.0,
    )
    summary = await run_matrix(args)

    assert controllers[0].restore_calls == 2
    assert summary["results"]["N0"]["summary"]["business_retention"]["rate"] == 1.0
    assert summary["results"]["CF5"]["recovery"]["recovered"] is True
    assert "probabilistic TCP connection reset" in summary["important_limitation"]
    assert summary["fault_timeline"][-1]["event"] == "final_restore"
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")) == summary
    assert (tmp_path / "N0.json").is_file()
    assert (tmp_path / "CF5.json").is_file()


@pytest.mark.asyncio
async def test_matrix_restores_proxy_when_a_benchmark_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    restore_calls = 0

    class FakeController:
        def __init__(self, _api_url: str) -> None:
            pass

        async def apply(self, profile) -> dict:
            return {"enabled": profile.enabled, "toxics": []}

        async def restore(self) -> dict:
            nonlocal restore_calls
            restore_calls += 1
            return {"enabled": True, "toxics": []}

    async def failed_benchmark(_config: BenchmarkConfig) -> dict:
        raise RuntimeError("load generator failed")

    monkeypatch.setattr("scripts.run_weak_network_matrix.ToxiproxyController", FakeController)
    monkeypatch.setattr("scripts.run_weak_network_matrix.run_benchmark", failed_benchmark)
    args = Namespace(
        output_dir=tmp_path,
        toxiproxy_api_url="http://toxiproxy:8474",
        profiles=["N0"],
        url="http://edge-a/v1/tasks",
        requests=1,
        concurrency=1,
        scene="industrial",
        deadline_ms=200,
        timeout_ms=1000,
        recovery_window_requests=1,
        recovery_timeout_s=1.0,
    )

    with pytest.raises(RuntimeError, match="load generator failed"):
        await run_matrix(args)

    assert restore_calls == 1
