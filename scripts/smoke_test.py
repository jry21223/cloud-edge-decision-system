from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

EDGE_URL = os.getenv("EDGE_URL", "http://localhost:8001").rstrip("/")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8002").rstrip("/")
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
        "低置信度远端协同",
        {
            "task_id": "smoke-peer",
            "scene": "industrial",
            "payload": {"temperature": 84, "vibration": 7.2, "current": 16.2, "log": "间歇异响"},
            # Medium risk keeps the low-confidence task eligible for remote
            # collaboration while making the nearby healthy Peer preferable
            # to the higher-latency Cloud in DREAM-Route's cost function.
            "risk_level": "medium",
            "deadline_ms": 900,
            "metadata": {"force_confidence": 0.55},
        },
        "PEER_EDGE",
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


CLOUD_ESCALATION: dict[str, Any] = {
    "task": {
        "task_id": "smoke-cloud",
        "scene": "industrial",
        "payload": {"temperature": 84, "vibration": 7.2, "current": 16.2, "log": "间歇异响"},
        "risk_level": "high",
        "deadline_ms": 900,
        "metadata": {"cloud_estimated_latency_ms": 350},
    },
    "edge_result": {
        "prediction": "warning",
        "confidence": 0.55,
        "action": "inspect",
        "reason": "低置信度夹具，用于隔离验证 Controller 的 CLOUD 路径",
        "latency_ms": 5.0,
        "model_name": "smoke-fixture",
        "node_id": "edge-a",
    },
    "elapsed_ms": 5.0,
    "origin_node": "edge-a",
    "hop_count": 1,
    # Explicitly exclude both registered peers. This isolates the CLOUD path
    # instead of relying on timing or temporarily disabling a healthy service.
    "visited_nodes": ["edge-a", "edge-b"],
}


def set_cloud_enabled(enabled: bool) -> bool:
    try:
        response = httpx.post(
            f"{TOXIPROXY_API_URL}/proxies/cloud",
            json={"enabled": enabled},
            timeout=1.5,
        )
        response.raise_for_status()
        state = httpx.get(
            f"{TOXIPROXY_API_URL}/proxies/cloud",
            timeout=1.5,
        )
        state.raise_for_status()
        return bool(state.json()["enabled"]) is enabled
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
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


def run_cloud_case(client: httpx.Client) -> bool:
    response = client.post(f"{CONTROLLER_URL}/v1/escalate", json=CLOUD_ESCALATION)
    response.raise_for_status()
    result = response.json()
    ok = result["route"] == "CLOUD"
    print(
        f"[{'PASS' if ok else 'FAIL'}] 独立云端增强路径: "
        f"{result['route']} ({result['total_latency_ms']} ms)"
    )
    if not ok:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return ok


def run_disconnected_cloud_case(client: httpx.Client) -> bool:
    payload = json.loads(json.dumps(CLOUD_ESCALATION))
    payload["task"]["task_id"] = "smoke-fallback"
    response = client.post(f"{CONTROLLER_URL}/v1/escalate", json=payload)
    response.raise_for_status()
    result = response.json()
    ok = result["route"] == "EDGE_FALLBACK"
    print(
        f"[{'PASS' if ok else 'FAIL'}] 云端断开本地降级: "
        f"{result['route']} ({result['total_latency_ms']} ms)"
    )
    if not ok:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return ok


def wait_for_peer(client: httpx.Client) -> bool:
    for _attempt in range(30):
        try:
            response = client.get(f"{CONTROLLER_URL}/v1/nodes")
            response.raise_for_status()
            healthy_nodes = [node for node in response.json() if node["healthy"]]
            if len(healthy_nodes) >= 2:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    passed = True
    with httpx.Client(timeout=3.0) as client:
        peer_ready = wait_for_peer(client)
        if not peer_ready:
            print("[FAIL] 两个健康边缘节点尚未注册，无法证明 PEER_EDGE 路径")
            passed = False
        for title, payload, expected in CASES:
            passed &= run_case(client, title, payload, expected)

        # The regular low-confidence case intentionally prefers a healthy Peer.
        # Exercise Cloud independently so one run proves all five route classes.
        passed &= run_cloud_case(client)

        if set_cloud_enabled(False):
            try:
                passed &= run_disconnected_cloud_case(client)
            finally:
                if not set_cloud_enabled(True):
                    print("[FAIL] Toxiproxy 云端链路未能恢复")
                    passed = False
        else:
            print("[FAIL] 无法访问或验证 Toxiproxy，不能跳过断网测试")
            passed = False

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
