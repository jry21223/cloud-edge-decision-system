from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager, suppress
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, HTTPException

from common.recorder_client import flush_pending_events, record_event
from common.schemas import InferenceResult, NodeHeartbeat, TaskRequest
from common.telemetry import RuntimeTelemetry
from common.vision_runtime import build_cloud_vision_adapter
from services.cloud_node.core import infer_industrial, infer_traffic

DELAY_MS = int(os.getenv("CLOUD_INFERENCE_DELAY_MS", "350"))
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8002").rstrip("/")
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "2"))
_VISION_ADAPTER = build_cloud_vision_adapter()
_IDEMPOTENCY_CACHE_LIMIT = 2048
_idempotency_cache: dict[str, InferenceResult] = {}
_inflight: dict[str, asyncio.Task[InferenceResult]] = {}
_request_hash_by_key: dict[str, str] = {}
_idempotency_lock = asyncio.Lock()
runtime_telemetry = RuntimeTelemetry(
    capacity=int(os.getenv("CLOUD_MAX_CONCURRENCY", "4")),
    initial_latency_ms=max(20.0, float(DELAY_MS)),
)


def heartbeat_payload() -> NodeHeartbeat:
    snapshot = runtime_telemetry.snapshot()
    return NodeHeartbeat(
        node_id="cloud-node",
        endpoint_url=None,
        supported_scenes=[],
        load=snapshot.load,
        queue_depth=snapshot.queue_depth,
        estimated_latency_ms=snapshot.estimated_latency_ms,
        model_version="cloud-rule+vision-baseline-v1",
        reliability=0.96,
        cpu_utilization=snapshot.cpu_utilization,
        memory_utilization=snapshot.memory_utilization,
        process_rss_mb=snapshot.process_rss_mb,
        gpu_utilization=snapshot.gpu_utilization,
        gpu_memory_used_mb=snapshot.gpu_memory_used_mb,
        telemetry_source=snapshot.source,
    )


async def heartbeat_loop() -> None:
    while True:
        try:
            payload = await asyncio.to_thread(heartbeat_payload)
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.post(
                    f"{CONTROLLER_URL}/v1/nodes/heartbeat",
                    json=payload.model_dump(mode="json"),
                    headers={
                        "X-Node-Registration-Token": os.getenv(
                            "NODE_REGISTRATION_TOKEN", ""
                        )
                    },
                )
        except (httpx.HTTPError, OSError, ValueError):
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        yield
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await flush_pending_events()


app = FastAPI(title="Cloud-Edge MVP - Cloud Node", version="0.2.0", lifespan=lifespan)


def _request_hash(task: TaskRequest, *, review_only: bool) -> str:
    canonical = json.dumps(
        {"task": task.model_dump(mode="json"), "review_only": review_only},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.get("/health")
async def health() -> dict[str, object]:
    snapshot = await asyncio.to_thread(runtime_telemetry.snapshot)
    return {
        "status": "ok",
        "model": "cloud-fusion-model-v0.1",
        "delay_ms": DELAY_MS,
        "telemetry": snapshot.__dict__,
    }


async def _perform_inference(task: TaskRequest, *, review_only: bool) -> InferenceResult:
    started = time.perf_counter()
    await asyncio.sleep(DELAY_MS / 1000)

    if task.image is not None:
        result = await asyncio.to_thread(_VISION_ADAPTER.infer, task, node_id="cloud-node")
        result = result.model_copy(
            update={"latency_ms": round((time.perf_counter() - started) * 1000, 3)}
        )
    elif task.scene == "traffic":
        prediction, action, reason = infer_traffic(task.payload)
        result = InferenceResult(
            prediction=prediction,
            confidence=0.96,
            action=action,
            reason=reason,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            model_name="cloud-fusion-model-v0.1",
            node_id="cloud-node",
        )
    else:
        prediction, action, reason = infer_industrial(task.payload)
        result = InferenceResult(
            prediction=prediction,
            confidence=0.96,
            action=action,
            reason=reason,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            model_name="cloud-fusion-model-v0.1",
            node_id="cloud-node",
        )
    await record_event(
        task_id=task.task_id,
        component="cloud-node",
        event_type="late_review" if review_only else "inference",
        route="CLOUD",
        data={
            **result.model_dump(mode="json"),
            "trace_id": task.trace_id,
            "workpiece_id": task.workpiece_id,
            "image_sha256": task.image.sha256 if task.image else None,
            "review_only": review_only,
        },
    )
    return result


@app.post("/v1/infer", response_model=InferenceResult)
async def infer(
    task: TaskRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    review_only: Annotated[bool, Header(alias="X-Review-Only")] = False,
) -> InferenceResult:
    """Run Cloud inference once per idempotency key.

    A recovered Edge outbox sends ``X-Review-Only: true``. Such a late result is
    evidence only and cannot overwrite the action already committed at Edge.
    """

    started = time.perf_counter()
    await runtime_telemetry.acquire_task()
    try:
        return await _infer_idempotent(
            task,
            idempotency_key=idempotency_key,
            review_only=review_only,
        )
    finally:
        runtime_telemetry.release_task(
            latency_ms=(time.perf_counter() - started) * 1000
        )


async def _infer_idempotent(
    task: TaskRequest,
    *,
    idempotency_key: str | None,
    review_only: bool,
) -> InferenceResult:
    if not idempotency_key:
        return await _perform_inference(task, review_only=review_only)

    request_hash = _request_hash(task, review_only=review_only)
    async with _idempotency_lock:
        known_hash = _request_hash_by_key.get(idempotency_key)
        if known_hash is not None and known_hash != request_hash:
            raise HTTPException(
                status_code=409,
                detail="idempotency key is already bound to different request content",
            )
        _request_hash_by_key[idempotency_key] = request_hash
        cached = _idempotency_cache.get(idempotency_key)
        if cached is not None:
            return cached
        pending = _inflight.get(idempotency_key)
        if pending is None:
            pending = asyncio.create_task(_perform_inference(task, review_only=review_only))
            _inflight[idempotency_key] = pending

    try:
        result = await asyncio.shield(pending)
    finally:
        if pending.done():
            async with _idempotency_lock:
                _inflight.pop(idempotency_key, None)

    async with _idempotency_lock:
        _idempotency_cache[idempotency_key] = result
        while len(_idempotency_cache) > _IDEMPOTENCY_CACHE_LIMIT:
            oldest_key = next(iter(_idempotency_cache))
            _idempotency_cache.pop(oldest_key)
            _request_hash_by_key.pop(oldest_key, None)
    return result
