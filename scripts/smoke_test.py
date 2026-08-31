from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import time
from typing import Any

import httpx
from PIL import Image, ImageDraw

EDGE_URL = os.getenv("EDGE_URL", "http://localhost:8001").rstrip("/")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8002").rstrip("/")
TOXIPROXY_API_URL = os.getenv("TOXIPROXY_API_URL", "http://localhost:8474").rstrip("/")
RECORDER_URL = os.getenv("RECORDER_URL", "http://localhost:8004").rstrip("/")

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
    # The MVP does not route through Peer Edge. hop_count=1 also isolates Cloud
    # when this smoke test is run with the optional Peer extension enabled.
    "visited_nodes": ["edge-a"],
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


def _traffic_visual_payload(task_id: str) -> dict[str, Any]:
    image = Image.new("RGB", (96, 72), (125, 125, 125))
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((8, 8, 40, 28), fill=(180, 180, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    return {
        "task_id": task_id,
        "scene": "traffic",
        "payload": {},
        "context": {"data_provenance": "synthetic_architecture_probe"},
        "risk_level": "medium",
        "deadline_ms": 2_000,
        "metadata": {
            "allow_raw_upload": True,
            "force_confidence": 0.55,
            "cloud_expected_accuracy": 0.99,
        },
        "image": {
            "frame_id": "smoke-traffic-frame",
            "width": 96,
            "height": 72,
            "mime_type": "image/png",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "data_base64": base64.b64encode(raw).decode("ascii"),
        },
    }


def _recorder_has_decision(client: httpx.Client, task_id: str) -> bool:
    for _attempt in range(20):
        events = client.get(f"{RECORDER_URL}/v1/events", params={"limit": 100})
        events.raise_for_status()
        if any(
            item["task_id"] == task_id and item["event_type"] == "decision"
            for item in events.json()
        ):
            return True
        time.sleep(0.25)
    return False


def run_traffic_visual_migration_case(client: httpx.Client) -> bool:
    payload = _traffic_visual_payload("smoke-traffic-vision")
    response = client.post(f"{EDGE_URL}/v1/tasks", json=payload)
    response.raise_for_status()
    result = response.json()
    route_ok = result["route"] == "CLOUD"
    adapter_ok = (
        result["edge_result"]["model_name"] == "traffic-vision-migration-probe"
        and result["cloud_result"]["model_name"] == "traffic-vision-migration-probe"
    )
    recorded = _recorder_has_decision(client, str(payload["task_id"]))
    ok = route_ok and adapter_ok and recorded
    print(
        f"[{'PASS' if ok else 'FAIL'}] 交通视觉架构迁移: "
        f"route={result['route']}, recorder={recorded}"
    )
    if not ok:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return ok


def _remove_traffic_timeout_toxic() -> bool:
    try:
        response = httpx.delete(
            f"{TOXIPROXY_API_URL}/proxies/cloud/toxics/traffic-visual-timeout",
            timeout=1.5,
        )
        return response.status_code in {204, 404}
    except httpx.HTTPError:
        return False


def run_traffic_visual_timeout_case(client: httpx.Client) -> bool:
    _remove_traffic_timeout_toxic()
    created = httpx.post(
        f"{TOXIPROXY_API_URL}/proxies/cloud/toxics",
        json={
            "name": "traffic-visual-timeout",
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": 1000, "jitter": 0},
        },
        timeout=1.5,
    )
    if created.status_code != 200:
        print("[FAIL] 无法创建交通视觉 Cloud 超时故障")
        return False
    payload = _traffic_visual_payload("smoke-traffic-vision-timeout")
    ok = False
    try:
        response = client.post(f"{EDGE_URL}/v1/tasks", json=payload)
        response.raise_for_status()
        result = response.json()
        recorded = _recorder_has_decision(client, str(payload["task_id"]))
        ok = (
            result["route"] == "EDGE_FALLBACK"
            and "CLOUD" in result["attempted_routes"]
            and recorded
        )
        print(
            f"[{'PASS' if ok else 'FAIL'}] 交通视觉 Cloud 超时回退: "
            f"route={result['route']}, recorder={recorded}"
        )
        if not ok:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        removed = _remove_traffic_timeout_toxic()
        if not removed:
            print("[FAIL] 未能清理交通视觉超时故障")
    return ok and removed


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


def wait_for_edge(client: httpx.Client) -> bool:
    for _attempt in range(60):
        try:
            response = client.get(f"{EDGE_URL}/health")
            response.raise_for_status()
            if response.json().get("status") == "ok":
                return True
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    passed = True
    with httpx.Client(timeout=3.0) as client:
        if not wait_for_edge(client):
            print("[FAIL] Edge health endpoint did not become readable")
            return 1
        for title, payload, expected in CASES:
            passed &= run_case(client, title, payload, expected)

        # Exercise Cloud independently so one run proves the four MVP route classes.
        passed &= run_cloud_case(client)
        passed &= run_traffic_visual_migration_case(client)
        passed &= run_traffic_visual_timeout_case(client)

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
