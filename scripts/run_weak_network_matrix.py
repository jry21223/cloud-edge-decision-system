from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

try:
    from scripts.benchmark_system import BenchmarkConfig, run_benchmark
except ModuleNotFoundError as error:  # Direct ``python scripts/...`` execution.
    if error.name != "scripts":
        raise
    from benchmark_system import BenchmarkConfig, run_benchmark


@dataclass(frozen=True)
class Toxic:
    name: str
    toxic_type: str
    stream: str
    toxicity: float
    attributes: dict[str, Any]


@dataclass(frozen=True)
class WeakNetworkProfile:
    name: str
    enabled: bool = True
    toxics: tuple[Toxic, ...] = ()
    interpretation: str = ""


PROFILES = {
    "N0": WeakNetworkProfile(name="N0", interpretation="proxy enabled; no added fault"),
    "RTT100": WeakNetworkProfile(
        name="RTT100",
        toxics=(
            Toxic("latency-up", "latency", "upstream", 1.0, {"latency": 50, "jitter": 0}),
            Toxic(
                "latency-down", "latency", "downstream", 1.0, {"latency": 50, "jitter": 0}
            ),
        ),
        interpretation="approximately 100ms added round-trip latency; verify with observed RTT",
    ),
    "RTT300": WeakNetworkProfile(
        name="RTT300",
        toxics=(
            Toxic("latency-up", "latency", "upstream", 1.0, {"latency": 150, "jitter": 0}),
            Toxic(
                "latency-down", "latency", "downstream", 1.0, {"latency": 150, "jitter": 0}
            ),
        ),
        interpretation="approximately 300ms added round-trip latency; verify with observed RTT",
    ),
    "BW5M": WeakNetworkProfile(
        name="BW5M",
        toxics=(
            Toxic("bandwidth-up", "bandwidth", "upstream", 1.0, {"rate": 625}),
            Toxic("bandwidth-down", "bandwidth", "downstream", 1.0, {"rate": 625}),
        ),
        interpretation="5Mbps nominal body bandwidth in each direction",
    ),
    "CF5": WeakNetworkProfile(
        name="CF5",
        toxics=(
            Toxic("connection-reset", "reset_peer", "downstream", 0.05, {"timeout": 0}),
        ),
        interpretation=(
            "5% probabilistic TCP connection reset; this is not packet loss and must not be "
            "reported as packet-loss percentage"
        ),
    ),
    "OFFLINE": WeakNetworkProfile(
        name="OFFLINE",
        enabled=False,
        interpretation="Cloud proxy disabled; deterministic outage",
    ),
}


class ToxiproxyController:
    def __init__(self, api_url: str, *, proxy_name: str = "cloud") -> None:
        self.api_url = api_url.rstrip("/")
        self.proxy_name = proxy_name

    async def _request(self, method: str, suffix: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.request(method, f"{self.api_url}{suffix}", **kwargs)
            response.raise_for_status()
            return response

    async def state(self) -> dict[str, Any]:
        response = await self._request("GET", f"/proxies/{self.proxy_name}")
        return response.json()

    async def clear_toxics(self) -> None:
        state = await self.state()
        for toxic in state.get("toxics", []):
            name = toxic.get("name")
            if name:
                await self._request(
                    "DELETE", f"/proxies/{self.proxy_name}/toxics/{name}"
                )

    async def set_enabled(self, enabled: bool) -> None:
        await self._request(
            "POST",
            f"/proxies/{self.proxy_name}",
            json={"enabled": enabled},
        )

    async def apply(self, profile: WeakNetworkProfile) -> dict[str, Any]:
        await self.clear_toxics()
        await self.set_enabled(True)
        for toxic in profile.toxics:
            await self._request(
                "POST",
                f"/proxies/{self.proxy_name}/toxics",
                json={
                    "name": toxic.name,
                    "type": toxic.toxic_type,
                    "stream": toxic.stream,
                    "toxicity": toxic.toxicity,
                    "attributes": toxic.attributes,
                },
            )
        await self.set_enabled(profile.enabled)
        state = await self.state()
        if bool(state.get("enabled")) != profile.enabled:
            raise RuntimeError(f"proxy enabled state does not match profile {profile.name}")
        observed = {str(item.get("name")): item for item in state.get("toxics", [])}
        if set(observed) != {toxic.name for toxic in profile.toxics}:
            raise RuntimeError(f"proxy toxic names do not match profile {profile.name}")
        for expected in profile.toxics:
            actual = observed[expected.name]
            if (
                actual.get("type") != expected.toxic_type
                or actual.get("stream") != expected.stream
                or abs(float(actual.get("toxicity", -1)) - expected.toxicity) > 1e-9
            ):
                raise RuntimeError(
                    f"proxy toxic contract does not match profile {profile.name}: {expected.name}"
                )
            attributes = actual.get("attributes", {})
            if any(attributes.get(key) != value for key, value in expected.attributes.items()):
                raise RuntimeError(
                    f"proxy toxic attributes do not match profile {profile.name}: {expected.name}"
                )
        return state

    async def restore(self) -> dict[str, Any]:
        return await self.apply(PROFILES["N0"])


async def measure_recovery(
    *,
    config: BenchmarkConfig,
    baseline_retention: float,
    consecutive_windows: int = 3,
    timeout_s: float = 60.0,
    window_seconds: float = 5.0,
) -> dict[str, Any]:
    if baseline_retention <= 0:
        raise ValueError("baseline retention must be positive for recovery measurement")
    started = time.perf_counter()
    consecutive = 0
    windows: list[dict[str, Any]] = []
    threshold = baseline_retention * 0.95
    while time.perf_counter() - started < timeout_s:
        window_started = time.perf_counter()
        eligible = 0
        retained = 0
        batches = 0
        while True:
            report = await run_benchmark(config)
            business = report["summary"]["business_retention"]
            eligible += int(business["eligible_tasks"])
            retained += int(business["retained_tasks"])
            batches += 1
            if time.perf_counter() - window_started >= window_seconds:
                break
            if time.perf_counter() - started >= timeout_s:
                break
        retention = 0.0 if eligible == 0 else retained / eligible
        elapsed_ms = (time.perf_counter() - started) * 1000
        windows.append(
            {
                "elapsed_ms": round(elapsed_ms, 3),
                "window_seconds": round(time.perf_counter() - window_started, 3),
                "batches": batches,
                "eligible_tasks": eligible,
                "retained_tasks": retained,
                "retention": retention,
            }
        )
        consecutive = consecutive + 1 if retention >= threshold else 0
        if consecutive >= consecutive_windows:
            return {
                "recovered": True,
                "recovery_time_ms": round(elapsed_ms, 3),
                "threshold": threshold,
                "windows": windows,
            }
    return {
        "recovered": False,
        "recovery_time_ms": None,
        "threshold": threshold,
        "windows": windows,
    }


async def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    controller = ToxiproxyController(args.toxiproxy_api_url)
    timeline: list[dict[str, Any]] = []
    reports: dict[str, dict[str, Any]] = {}
    baseline_retention: float | None = None

    if "N0" in args.profiles and args.profiles[0] != "N0":
        raise ValueError("N0 must be the first profile so recovery has a valid baseline")

    try:
        for name in args.profiles:
            profile = PROFILES[name]
            applied_at = datetime.now(UTC).isoformat()
            state = await controller.apply(profile)
            timeline.append(
                {
                    "at": applied_at,
                    "event": "profile_applied",
                    "profile": name,
                    "proxy_enabled": state.get("enabled"),
                    "toxics": state.get("toxics", []),
                }
            )
            config = BenchmarkConfig(
                url=args.url,
                request_count=args.requests,
                concurrency=args.concurrency,
                scene=args.scene,
                deadline_ms=args.deadline_ms,
                timeout_ms=args.timeout_ms,
                fault_profile=name,
                workload_mode="controller_cloud",
            )
            report = await run_benchmark(config)
            attempted = int(report["summary"].get("cloud_path_attempted_requests", 0))
            total = int(report["summary"]["total_requests"])
            if attempted != total:
                raise RuntimeError(
                    f"profile {name} did not exercise the Cloud proxy for every task "
                    f"({attempted}/{total})"
                )
            report["fault_profile"] = {
                **asdict(profile),
                "applied_proxy_state": state,
            }
            if name == "N0":
                baseline_retention = float(
                    report["summary"]["business_retention"]["rate"]
                )
                if baseline_retention <= 0:
                    raise RuntimeError("N0 retention is zero; recovery threshold is undefined")
            reports[name] = report
            (output_dir / f"{name}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            if name != "N0":
                restored = await controller.restore()
                timeline.append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "event": "baseline_restored",
                        "profile": name,
                        "proxy_enabled": restored.get("enabled"),
                        "toxics": restored.get("toxics", []),
                    }
                )
                if baseline_retention is not None:
                    recovery_config = BenchmarkConfig(
                        url=args.url,
                        request_count=args.recovery_window_requests,
                        concurrency=min(args.concurrency, args.recovery_window_requests),
                        scene=args.scene,
                        deadline_ms=args.deadline_ms,
                        timeout_ms=args.timeout_ms,
                        fault_profile="RECOVERY_N0",
                        workload_mode="controller_cloud",
                    )
                    report["recovery"] = await measure_recovery(
                        config=recovery_config,
                        baseline_retention=baseline_retention,
                        timeout_s=args.recovery_timeout_s,
                        window_seconds=getattr(args, "recovery_window_seconds", 5.0),
                    )
                    (output_dir / f"{name}.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
    finally:
        restored = await controller.restore()
        timeline.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "event": "final_restore",
                "proxy_enabled": restored.get("enabled"),
                "toxics": restored.get("toxics", []),
            }
        )

    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "profiles": list(args.profiles),
        "important_limitation": (
            "CF5 is a probabilistic TCP connection reset. Toxiproxy 2.12.0 does not "
            "provide packet-level loss; packet-loss claims require tc netem evidence."
        ),
        "results": {
            name: {
                "summary": report["summary"],
                "recovery": report.get("recovery"),
            }
            for name, report in reports.items()
        },
        "fault_timeline": timeline,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply verified Toxiproxy profiles and run the same labeled workload."
    )
    parser.add_argument("--url", default="http://localhost:8002/v1/escalate")
    parser.add_argument("--toxiproxy-api-url", default="http://localhost:8474")
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=tuple(PROFILES),
        default=["N0", "RTT100", "RTT300", "BW5M", "CF5", "OFFLINE"],
    )
    parser.add_argument("--requests", type=positive_int, default=30)
    parser.add_argument("--concurrency", type=positive_int, default=5)
    parser.add_argument("--scene", choices=("industrial", "traffic"), default="industrial")
    parser.add_argument("--deadline-ms", type=positive_int, default=500)
    parser.add_argument("--timeout-ms", type=positive_int, default=1200)
    parser.add_argument("--recovery-window-requests", type=positive_int, default=5)
    parser.add_argument("--recovery-window-seconds", type=positive_float, default=5.0)
    parser.add_argument("--recovery-timeout-s", type=float, default=60.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/results/weak-network-latest"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = asyncio.run(run_matrix(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
