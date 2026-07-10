from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

EDGE_URL = os.getenv("EDGE_URL", "http://localhost:8001").rstrip("/")
TOXIPROXY_API_URL = os.getenv("TOXIPROXY_API_URL", "http://localhost:8474").rstrip("/")

CASES: list[tuple[str, dict[str, Any], str]] = [
    (
        "高置信度本地处理",
        {
            "task_id": "smoke-edge",
            "scene": "industrial",
            "payload": {"temperature": 30, "vibration": 1.1, "current": 5.2},
            "risk_level": "low",
            "deadline_ms": 800,
            "metadata": {"force_confidence": 0.95},
        },
        "EDGE",
    ),
    (
        "低置信度正常上云",
        {
            "task_id": "smoke-cloud",
            "scene": "industrial",
            "payload": {"temperature": 84, "vibration": 7.2, "current": 16.2, "log": "间歇异响"},
            "risk_level": "high",
            "deadline_ms": 900,
            "metadata": {"force_confidence": 0.55},
        },
        "CLOUD",
    ),
    (
        "高风险边缘安全动作",
        {
            "task_id": "smoke-safety",
            "scene": "industrial",
            "payload": {"temperature": 96, "vibration": 8.4, "current": 18.5},
            "risk_level": "critical",
            "deadline_ms": 300,
        },
        "EDGE_SAFETY",
    ),
]


def set_cloud_enabled(enabled: bool) -> bool:
    try:
        response = httpx.post(
            f"{TOXIPROXY_API_URL}/proxies/cloud",
            json={"enabled": enabled},
            timeout=1.5,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def run_case(client: httpx.Client, title: str, payload: dict[str, Any], expected: str) -> bool:
    response = client.post(f"{EDGE_URL}/v1/tasks", json=payload)
    response.raise_for_status()
    result = response.json()
    ok = result["route"] == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {title}: {result['route']} ({result['total_latency_ms']} ms)")
    if not ok:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return ok


def main() -> int:
    passed = True
    with httpx.Client(timeout=3.0) as client:
        for title, payload, expected in CASES:
            passed &= run_case(client, title, payload, expected)

        if set_cloud_enabled(False):
            try:
                fallback = {
                    "task_id": "smoke-fallback",
                    "scene": "industrial",
                    "payload": {"temperature": 74, "vibration": 4.9, "current": 13.8},
                    "risk_level": "medium",
                    "deadline_ms": 500,
                    "metadata": {"force_confidence": 0.55},
                }
                passed &= run_case(client, "云端断开本地降级", fallback, "EDGE_FALLBACK")
            finally:
                set_cloud_enabled(True)
        else:
            print("[SKIP] 无法访问 Toxiproxy API，跳过断网测试")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
