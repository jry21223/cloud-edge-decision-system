from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class TelemetrySnapshot:
    load: float
    queue_depth: int
    estimated_latency_ms: float
    cpu_utilization: float
    memory_utilization: float
    process_rss_mb: float
    gpu_utilization: float | None
    gpu_memory_used_mb: float | None
    rtt_ms: float | None
    bandwidth_mbps: float | None
    source: str = "runtime"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class RuntimeTelemetry:
    """Thread-safe live resource and observed-network sampler."""

    def __init__(self, *, capacity: int = 4, initial_latency_ms: float = 20.0) -> None:
        self.capacity = max(1, capacity)
        self._lock = threading.Lock()
        self._inflight = 0
        self._waiting = 0
        self._semaphore = asyncio.Semaphore(self.capacity)
        self._latency_ewma_ms = max(0.0, initial_latency_ms)
        self._rtt_ewma_ms: float | None = None
        self._bandwidth_ewma_mbps: float | None = None
        self._gpu_cache: tuple[float, float | None, float | None] = (0.0, None, None)
        self._process = psutil.Process()
        self._process.cpu_percent(None)

    def task_started(self) -> None:
        with self._lock:
            self._inflight += 1

    def task_finished(self, *, latency_ms: float | None = None) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if latency_ms is not None and latency_ms >= 0:
                self._latency_ewma_ms = 0.25 * latency_ms + 0.75 * self._latency_ewma_ms

    async def acquire_task(self) -> None:
        """Enter the bounded inference gate and expose real waiting depth."""

        with self._lock:
            self._waiting += 1
        try:
            await self._semaphore.acquire()
        except BaseException:
            with self._lock:
                self._waiting = max(0, self._waiting - 1)
            raise
        with self._lock:
            self._waiting = max(0, self._waiting - 1)
            self._inflight += 1

    def release_task(self, *, latency_ms: float | None = None) -> None:
        self.task_finished(latency_ms=latency_ms)
        self._semaphore.release()

    def observe_network(
        self,
        *,
        elapsed_ms: float,
        transferred_bytes: int = 0,
        success: bool = True,
    ) -> None:
        if elapsed_ms < 0:
            return
        observed_rtt = elapsed_ms if success else max(elapsed_ms, 1_000.0)
        bandwidth_mbps = None
        if success and elapsed_ms > 0 and transferred_bytes > 0:
            bandwidth_mbps = transferred_bytes * 8 / (elapsed_ms / 1000.0) / 1_000_000
        with self._lock:
            self._rtt_ewma_ms = (
                observed_rtt
                if self._rtt_ewma_ms is None
                else 0.25 * observed_rtt + 0.75 * self._rtt_ewma_ms
            )
            if bandwidth_mbps is not None:
                self._bandwidth_ewma_mbps = (
                    bandwidth_mbps
                    if self._bandwidth_ewma_mbps is None
                    else 0.25 * bandwidth_mbps + 0.75 * self._bandwidth_ewma_mbps
                )

    def _gpu_snapshot(self) -> tuple[float | None, float | None]:
        now = time.monotonic()
        cached_at, cached_utilization, cached_memory = self._gpu_cache
        if now - cached_at < 5.0:
            return cached_utilization, cached_memory
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            first_line = completed.stdout.strip().splitlines()[0]
            utilization_text, memory_text = (part.strip() for part in first_line.split(",", 1))
            utilization = _clamp(float(utilization_text) / 100.0)
            memory = max(0.0, float(memory_text))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            utilization = None
            memory = None
        self._gpu_cache = (now, utilization, memory)
        return utilization, memory

    def snapshot(self) -> TelemetrySnapshot:
        process_cpu = _clamp(self._process.cpu_percent(None) / 100.0)
        memory = psutil.virtual_memory()
        memory_utilization = _clamp(memory.percent / 100.0)
        process_rss_mb = self._process.memory_info().rss / (1024 * 1024)
        gpu_utilization, gpu_memory = self._gpu_snapshot()
        with self._lock:
            inflight = self._inflight
            waiting = self._waiting
            latency = self._latency_ewma_ms
            rtt = self._rtt_ewma_ms
            bandwidth = self._bandwidth_ewma_mbps
        queue_depth = max(waiting, inflight - self.capacity, 0)
        concurrency_load = _clamp(inflight / self.capacity)
        load = max(process_cpu, concurrency_load, memory_utilization * 0.50)
        return TelemetrySnapshot(
            load=round(_clamp(load), 6),
            queue_depth=queue_depth,
            estimated_latency_ms=round(max(0.0, latency), 6),
            cpu_utilization=round(process_cpu, 6),
            memory_utilization=round(memory_utilization, 6),
            process_rss_mb=round(process_rss_mb, 3),
            gpu_utilization=None if gpu_utilization is None else round(gpu_utilization, 6),
            gpu_memory_used_mb=None if gpu_memory is None else round(gpu_memory, 3),
            rtt_ms=None if rtt is None else round(max(0.0, rtt), 6),
            bandwidth_mbps=(
                None if bandwidth is None else round(max(0.0, bandwidth), 6)
            ),
        )
