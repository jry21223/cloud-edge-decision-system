from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class BenchmarkConfig:
    url: str
    request_count: int
    concurrency: int
    scene: str
    deadline_ms: int
    timeout_ms: int

    def __post_init__(self) -> None:
        if self.request_count <= 0:
            raise ValueError("request_count must be positive")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if self.scene not in {"industrial", "traffic"}:
            raise ValueError("scene must be 'industrial' or 'traffic'")
        if self.deadline_ms <= 0:
            raise ValueError("deadline_ms must be positive")
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")


@dataclass(frozen=True)
class RequestResult:
    index: int
    success: bool
    latency_ms: float
    route: str | None
    request_bytes: int
    response_bytes: int
    status_code: int | None
    error: str | None = None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Return a linearly interpolated percentile for q in the inclusive range 0..100."""
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    if not values:
        return None

    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * q / 100
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def summarize_results(
    results: Sequence[RequestResult],
    *,
    deadline_ms: int,
    elapsed_s: float,
) -> dict[str, Any]:
    """Aggregate client-observed results without inferring any server-side behavior."""
    total = len(results)
    successful = sum(result.success for result in results)
    deadline_met = sum(
        result.success and result.latency_ms <= deadline_ms for result in results
    )
    latencies = [result.latency_ms for result in results]
    routes = Counter(result.route or "UNKNOWN" for result in results if result.success)
    request_bytes = sum(result.request_bytes for result in results)
    response_bytes = sum(result.response_bytes for result in results)

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 6)

    safe_elapsed_s = max(float(elapsed_s), 0.0)
    return {
        "total_requests": total,
        "successful_requests": successful,
        "failed_requests": total - successful,
        "success_rate": 0.0 if total == 0 else successful / total,
        "deadline_met_requests": deadline_met,
        "deadline_met_rate": 0.0 if total == 0 else deadline_met / total,
        "route_distribution": dict(sorted(routes.items())),
        "request_bytes_total": request_bytes,
        "response_bytes_total": response_bytes,
        "traffic_bytes_total": request_bytes + response_bytes,
        "elapsed_s": round(safe_elapsed_s, 6),
        "throughput_rps": 0.0 if safe_elapsed_s == 0 else total / safe_elapsed_s,
        "successful_throughput_rps": (
            0.0 if safe_elapsed_s == 0 else successful / safe_elapsed_s
        ),
        "latency_ms": {
            "count": len(latencies),
            "mean": rounded(statistics.fmean(latencies) if latencies else None),
            "p50": rounded(percentile(latencies, 50)),
            "p95": rounded(percentile(latencies, 95)),
            "p99": rounded(percentile(latencies, 99)),
        },
    }


def build_task(index: int, *, scene: str, deadline_ms: int) -> dict[str, Any]:
    """Build a deterministic three-case workload for the selected scene."""
    if scene == "industrial":
        cases = (
            ("normal", "low", {"temperature": 30, "vibration": 1.1, "current": 5.2}),
            ("warning", "medium", {"temperature": 80, "vibration": 6.0, "current": 15.0}),
            ("critical", "critical", {"temperature": 95, "vibration": 9.0, "current": 19.0}),
        )
    else:
        cases = (
            (
                "normal",
                "low",
                {"vehicle_density": 0.2, "average_speed": 45, "queue_length": 5},
            ),
            (
                "congested",
                "medium",
                {"vehicle_density": 0.75, "average_speed": 18, "queue_length": 25},
            ),
            (
                "incident",
                "critical",
                {
                    "vehicle_density": 0.95,
                    "average_speed": 3,
                    "queue_length": 55,
                    "accident_reported": True,
                },
            ),
        )

    case_name, risk_level, payload = cases[index % len(cases)]
    return {
        "task_id": f"benchmark-{scene}-{index:06d}",
        "scene": scene,
        "payload": payload,
        "risk_level": risk_level,
        "deadline_ms": deadline_ms,
        "metadata": {"benchmark_case": case_name},
    }


async def _request_once(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    config: BenchmarkConfig,
    index: int,
) -> RequestResult:
    task = build_task(index, scene=config.scene, deadline_ms=config.deadline_ms)
    request_body = json.dumps(
        task,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.post(
                config.url,
                content=request_body,
                headers={"content-type": "application/json"},
            )
            response_body = await response.aread()
            latency_ms = (time.perf_counter() - started) * 1000
            if not response.is_success:
                return RequestResult(
                    index=index,
                    success=False,
                    latency_ms=round(latency_ms, 6),
                    route=None,
                    request_bytes=len(request_body),
                    response_bytes=len(response_body),
                    status_code=response.status_code,
                    error=f"HTTP {response.status_code}",
                )

            try:
                response_data = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                return RequestResult(
                    index=index,
                    success=False,
                    latency_ms=round(latency_ms, 6),
                    route=None,
                    request_bytes=len(request_body),
                    response_bytes=len(response_body),
                    status_code=response.status_code,
                    error=f"Invalid JSON response: {error}",
                )

            route_value = response_data.get("route") if isinstance(response_data, dict) else None
            return RequestResult(
                index=index,
                success=True,
                latency_ms=round(latency_ms, 6),
                route=str(route_value) if route_value is not None else "UNKNOWN",
                request_bytes=len(request_body),
                response_bytes=len(response_body),
                status_code=response.status_code,
            )
        except httpx.HTTPError as error:
            latency_ms = (time.perf_counter() - started) * 1000
            return RequestResult(
                index=index,
                success=False,
                latency_ms=round(latency_ms, 6),
                route=None,
                request_bytes=len(request_body),
                response_bytes=0,
                status_code=None,
                error=f"{type(error).__name__}: {error}",
            )


async def run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    """Run the workload against the configured URL; no network faults are injected."""
    semaphore = asyncio.Semaphore(config.concurrency)
    limits = httpx.Limits(
        max_connections=config.concurrency,
        max_keepalive_connections=config.concurrency,
    )
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.timeout_ms / 1000, limits=limits) as client:
        results = await asyncio.gather(
            *(
                _request_once(client, semaphore, config, index)
                for index in range(config.request_count)
            )
        )
    elapsed_s = time.perf_counter() - started

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "configuration": asdict(config),
        "measurement_notes": {
            "latency_scope": "client POST start through complete response body; semaphore wait excluded",
            "byte_scope": "HTTP request and response body bytes; protocol headers excluded",
            "deadline_rate_definition": "successful responses within deadline / all requests",
            "network_fault_injection": "not performed by this runner",
        },
        "summary": summarize_results(
            results,
            deadline_ms=config.deadline_ms,
            elapsed_s=elapsed_s,
        ),
        "requests": [asdict(result) for result in sorted(results, key=lambda item: item.index)],
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run client-side concurrent HTTP pressure measurements against the cloud-edge API. "
            "This runner observes the current environment and does not inject network faults."
        )
    )
    parser.add_argument("--url", default="http://localhost:8001/v1/tasks")
    parser.add_argument("--requests", type=positive_int, default=100, dest="request_count")
    parser.add_argument("--concurrency", type=positive_int, default=10)
    parser.add_argument("--scene", choices=("industrial", "traffic"), default="industrial")
    parser.add_argument("--deadline-ms", type=positive_int, default=200)
    parser.add_argument(
        "--timeout-ms",
        type=positive_int,
        help="Per-request client timeout; defaults to max(2 * deadline, 1000ms).",
    )
    parser.add_argument("--output", type=Path, help="Optional path for the JSON report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timeout_ms = args.timeout_ms or max(args.deadline_ms * 2, 1000)
    config = BenchmarkConfig(
        url=args.url,
        request_count=args.request_count,
        concurrency=args.concurrency,
        scene=args.scene,
        deadline_ms=args.deadline_ms,
        timeout_ms=timeout_ms,
    )
    report = asyncio.run(run_benchmark(config))
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
