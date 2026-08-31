from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from common.schemas import NodeHeartbeat
from common.telemetry import RuntimeTelemetry


class _FakeProcess:
    def __init__(self, *, cpu_percent: float = 25.0, rss_mb: float = 128.0) -> None:
        self.cpu_percent_value = cpu_percent
        self.rss_bytes = int(rss_mb * 1024 * 1024)

    def cpu_percent(self, _interval=None) -> float:
        return self.cpu_percent_value

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=self.rss_bytes)


def _fix_resource_samples(
    telemetry: RuntimeTelemetry,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cpu_percent: float = 25.0,
    memory_percent: float = 40.0,
    rss_mb: float = 128.0,
    gpu: tuple[float | None, float | None] = (None, None),
) -> None:
    telemetry._process = _FakeProcess(cpu_percent=cpu_percent, rss_mb=rss_mb)
    monkeypatch.setattr(
        "common.telemetry.psutil.virtual_memory",
        lambda: SimpleNamespace(percent=memory_percent),
    )
    monkeypatch.setattr(telemetry, "_gpu_snapshot", lambda: gpu)


def test_runtime_snapshot_reports_resources_queue_and_service_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = RuntimeTelemetry(capacity=0)
    _fix_resource_samples(
        telemetry,
        monkeypatch,
        cpu_percent=250.0,
        memory_percent=80.0,
        rss_mb=256.0,
        gpu=(0.25, 512.0),
    )

    assert telemetry.capacity == 1
    telemetry.task_started()
    telemetry.task_started()

    busy = telemetry.snapshot()
    assert busy.queue_depth == 1
    assert busy.load == 1.0
    assert busy.cpu_utilization == 1.0
    assert busy.memory_utilization == 0.8
    assert busy.process_rss_mb == 256.0
    assert busy.gpu_utilization == 0.25
    assert busy.gpu_memory_used_mb == 512.0
    assert busy.estimated_latency_ms == 20.0
    assert busy.source == "runtime"

    telemetry.task_finished(latency_ms=100.0)
    after_one_completion = telemetry.snapshot()
    assert after_one_completion.queue_depth == 0
    assert after_one_completion.estimated_latency_ms == 40.0

    telemetry.task_finished(latency_ms=-1.0)
    telemetry.task_finished()
    idle = telemetry.snapshot()
    assert idle.queue_depth == 0
    assert idle.estimated_latency_ms == 40.0


def test_network_observations_update_rtt_and_bandwidth_ewma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = RuntimeTelemetry(capacity=4)
    _fix_resource_samples(telemetry, monkeypatch)

    telemetry.observe_network(
        elapsed_ms=1000.0,
        transferred_bytes=1_000_000,
        success=True,
    )
    first = telemetry.snapshot()
    assert first.rtt_ms == 1000.0
    assert first.bandwidth_mbps == 8.0

    telemetry.observe_network(
        elapsed_ms=200.0,
        transferred_bytes=0,
        success=True,
    )
    second = telemetry.snapshot()
    assert second.rtt_ms == 800.0
    assert second.bandwidth_mbps == 8.0

    telemetry.observe_network(elapsed_ms=20.0, success=False)
    failed = telemetry.snapshot()
    assert failed.rtt_ms == 850.0
    assert failed.bandwidth_mbps == 8.0

    telemetry.observe_network(elapsed_ms=-1.0, transferred_bytes=99, success=True)
    assert telemetry.snapshot() == failed


def test_gpu_probe_is_cached_and_unavailable_gpu_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = RuntimeTelemetry()
    clock = {"now": 10.0}
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="40, 1024\n", stderr="")

    monkeypatch.setattr("common.telemetry.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("common.telemetry.subprocess.run", fake_run)

    assert telemetry._gpu_snapshot() == (0.4, 1024.0)
    clock["now"] = 12.0
    assert telemetry._gpu_snapshot() == (0.4, 1024.0)
    assert len(calls) == 1

    clock["now"] = 20.0
    monkeypatch.setattr(
        "common.telemetry.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert telemetry._gpu_snapshot() == (None, None)


def test_heartbeat_accepts_optional_runtime_fields_and_rejects_invalid_utilization() -> None:
    heartbeat = NodeHeartbeat(
        node_id="edge-a",
        cpu_utilization=0.5,
        memory_utilization=0.75,
        process_rss_mb=321.5,
        gpu_utilization=None,
        gpu_memory_used_mb=None,
        rtt_ms=15.0,
        bandwidth_mbps=80.0,
        telemetry_source="runtime",
    )

    assert heartbeat.gpu_utilization is None
    assert heartbeat.model_dump()["bandwidth_mbps"] == 80.0

    with pytest.raises(ValidationError):
        NodeHeartbeat(node_id="edge-a", cpu_utilization=float("nan"))

    with pytest.raises(ValidationError):
        NodeHeartbeat(node_id="edge-a", memory_utilization=1.01)
