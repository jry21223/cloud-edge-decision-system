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
    fault_profile: str | None = None
    workload_mode: str = "edge_tasks"

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
        if self.workload_mode not in {"edge_tasks", "controller_cloud"}:
            raise ValueError("workload_mode must be 'edge_tasks' or 'controller_cloud'")


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
    expected_prediction: str | None = None
    risk_level: str | None = None
    final_prediction: str | None = None
    final_action: str | None = None
    expected_action: str | None = None
    attempted_routes: tuple[str, ...] = ()


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
    labeled = [result for result in results if result.expected_prediction is not None]
    allowed_actions = {
        "continue",
        "inspect",
        "shutdown",
        "isolate",
        "quarantine",
        "keep_open",
        "divert",
        "close_lane",
        "slow_traffic",
    }
    retained = sum(
        result.success
        and result.latency_ms <= deadline_ms
        and result.final_action in allowed_actions
        for result in labeled
    )
    severe = [
        result
        for result in labeled
        if result.risk_level == "critical"
        or result.expected_prediction in {"critical", "incident"}
    ]
    safe_containment_actions = {"shutdown", "isolate", "quarantine", "close_lane"}
    severe_misses = sum(
        result.final_action not in safe_containment_actions for result in severe
    )
    normal = [result for result in labeled if result.expected_prediction == "normal"]
    false_isolations = sum(
        result.final_action in {"shutdown", "isolate", "quarantine", "close_lane"}
        for result in normal
    )
    action_matches = sum(
        result.final_action == result.expected_action
        for result in labeled
        if result.expected_action is not None
    )
    action_labeled = sum(result.expected_action is not None for result in labeled)
    cloud_path_attempted = sum("CLOUD" in result.attempted_routes for result in results)

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 6)

    safe_elapsed_s = max(float(elapsed_s), 0.0)
    summary = {
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
    if any(result.attempted_routes for result in results):
        summary["cloud_path_attempted_requests"] = cloud_path_attempted
    if labeled:
        summary["business_retention"] = {
            "eligible_tasks": len(labeled),
            "retained_tasks": retained,
            "rate": retained / len(labeled),
            "definition": "valid scene action returned within deadline / all injected tasks",
        }
        safety_summary = {
            "severe_tasks": len(severe),
            "severe_misses": severe_misses,
            "severe_miss_rate": 0.0 if not severe else severe_misses / len(severe),
            "normal_tasks": len(normal),
            "false_isolations": false_isolations,
            "false_isolation_rate": (
                0.0 if not normal else false_isolations / len(normal)
            ),
        }
        if action_labeled:
            safety_summary.update(
                {
                    "action_labeled_tasks": action_labeled,
                    "action_matches": action_matches,
                    "action_match_rate": action_matches / action_labeled,
                }
            )
        summary["safety"] = safety_summary
    return summary


def expected_case(index: int, *, scene: str) -> tuple[str, str, str]:
    if scene == "industrial":
        cases = (
            ("normal", "low", "continue"),
            ("warning", "medium", "inspect"),
            ("critical", "critical", "shutdown"),
        )
    else:
        cases = (
            ("normal", "low", "keep_open"),
            ("congested", "medium", "divert"),
            ("incident", "critical", "close_lane"),
        )
    return cases[index % len(cases)]


def build_task(
    index: int,
    *,
    scene: str,
    deadline_ms: int,
    workload_mode: str = "edge_tasks",
) -> dict[str, Any]:
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
    task = {
        "task_id": f"benchmark-{scene}-{index:06d}",
        "scene": scene,
        "payload": payload,
        "risk_level": risk_level,
        "deadline_ms": deadline_ms,
        "metadata": {"benchmark_case": case_name},
    }
    if workload_mode == "edge_tasks":
        return task
    if workload_mode != "controller_cloud":
        raise ValueError("unsupported workload_mode")
    # Isolate the Cloud data path so a Toxiproxy profile is actually exercised.
    # This is a labeled controller fixture, not an Edge model accuracy sample.
    return {
        "task": task,
        "edge_result": {
            "prediction": "uncertain",
            "confidence": 0.55,
            "action": "inspect",
            "reason": "cloud-path benchmark fixture",
            "latency_ms": 5.0,
            "model_name": "benchmark-fixture",
            "node_id": "edge-a",
        },
        "elapsed_ms": 5.0,
        "origin_node": "edge-a",
        "hop_count": 1,
        "visited_nodes": ["edge-a", "edge-b"],
    }


async def _request_once(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    config: BenchmarkConfig,
    index: int,
) -> RequestResult:
    task = build_task(
        index,
        scene=config.scene,
        deadline_ms=config.deadline_ms,
        workload_mode=config.workload_mode,
    )
    expected_prediction, risk_level, expected_action = expected_case(
        index,
        scene=config.scene,
    )
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
                    expected_prediction=expected_prediction,
                    risk_level=risk_level,
                    expected_action=expected_action,
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
                    expected_prediction=expected_prediction,
                    risk_level=risk_level,
                    expected_action=expected_action,
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
                expected_prediction=expected_prediction,
                risk_level=risk_level,
                expected_action=expected_action,
                final_prediction=str(response_data.get("final_prediction", "")),
                final_action=str(response_data.get("final_action", "")),
                attempted_routes=tuple(
                    str(item) for item in response_data.get("attempted_routes", [])
                ),
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
                expected_prediction=expected_prediction,
                risk_level=risk_level,
                expected_action=expected_action,
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
            "externally_applied_fault_profile": config.fault_profile,
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
        fault_profile=None,
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
