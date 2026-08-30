from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI

from common.fallback import conservative_fallback
from common.recorder_client import flush_pending_events, record_event
from common.schemas import (
    DecisionResponse,
    EscalationRequest,
    InferenceResult,
    NodeHeartbeat,
    Route,
    TaskRequest,
)
from services.edge_node.core import choose_local_route, infer_locally, safety_action_for
from services.edge_node.llm_adapter import warm_llm_if_enabled

NODE_ID = os.getenv("NODE_ID", "edge-a")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8002").rstrip("/")
NODE_ENDPOINT_URL = os.getenv("NODE_ENDPOINT_URL", f"http://{NODE_ID}:8000").rstrip("/")
LOCAL_THRESHOLD = float(os.getenv("LOCAL_CONFIDENCE_THRESHOLD", "0.80"))
MIN_REMOTE_BUDGET_MS = 80
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "5"))


def heartbeat_payload() -> NodeHeartbeat:
    return NodeHeartbeat(
        node_id=NODE_ID,
        endpoint_url=NODE_ENDPOINT_URL,
        load=float(os.getenv("NODE_LOAD", "0")),
        queue_depth=int(os.getenv("NODE_QUEUE_DEPTH", "0")),
        estimated_latency_ms=float(os.getenv("NODE_ESTIMATED_LATENCY_MS", "20")),
        model_version=os.getenv("NODE_MODEL_VERSION", "edge-rule-v0.1"),
    )


async def heartbeat_loop() -> None:
    while True:
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.post(
                    f"{CONTROLLER_URL}/v1/nodes/heartbeat",
                    json=heartbeat_payload().model_dump(mode="json"),
                )
        except (httpx.HTTPError, OSError, ValueError):
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("EDGE_LLM_WARM_ON_START", "false").lower() == "true":
        await asyncio.to_thread(warm_llm_if_enabled)
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        yield
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await flush_pending_events()


app = FastAPI(title="Cloud-Edge MVP - Edge Node", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "node_id": NODE_ID,
        "threshold": LOCAL_THRESHOLD,
        "inference_backend": os.getenv("EDGE_INFERENCE_BACKEND", "rule"),
    }


@app.post("/v1/infer")
async def infer(task: TaskRequest):
    return await asyncio.to_thread(infer_locally, task, NODE_ID)


async def edge_fallback_response(
    *,
    task: TaskRequest,
    edge_result: InferenceResult,
    started: float,
    trigger_reason: str,
) -> DecisionResponse:
    prediction, action, confidence, fallback_reason = conservative_fallback(task, edge_result)
    response = DecisionResponse(
        task_id=task.task_id,
        route=Route.EDGE_FALLBACK,
        final_prediction=prediction,
        final_action=action,
        final_confidence=confidence,
        decision_reason=f"{trigger_reason}；{fallback_reason}",
        edge_result=edge_result,
        degraded=True,
        total_latency_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    await record_event(
        task_id=task.task_id,
        component=NODE_ID,
        event_type="decision",
        route=response.route.value,
        data=response.model_dump(mode="json"),
    )
    return response


@app.post("/v1/tasks", response_model=DecisionResponse)
async def process_task(task: TaskRequest) -> DecisionResponse:
    started = time.perf_counter()
    # The optional GGUF backend is synchronous. Run all local inference in a
    # worker thread so one slow model call cannot block heartbeats or requests.
    edge_result = await asyncio.to_thread(infer_locally, task, NODE_ID)
    local_route = choose_local_route(task, edge_result, LOCAL_THRESHOLD)

    if local_route is not None:
        action = safety_action_for(task.scene) if local_route == Route.EDGE_SAFETY else edge_result.action
        reason = (
            "高风险任务由边缘端立即执行安全动作，不等待远端"
            if local_route == Route.EDGE_SAFETY
            else f"confidence={edge_result.confidence:.3f} ≥ threshold={LOCAL_THRESHOLD:.3f}"
        )
        response = DecisionResponse(
            task_id=task.task_id,
            route=local_route,
            final_prediction=edge_result.prediction,
            final_action=action,
            final_confidence=edge_result.confidence,
            decision_reason=reason,
            edge_result=edge_result,
            degraded=False,
            total_latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        await record_event(
            task_id=task.task_id,
            component=NODE_ID,
            event_type="decision",
            route=response.route.value,
            data=response.model_dump(mode="json"),
        )
        return response

    escalation = EscalationRequest(
        task=task,
        edge_result=edge_result,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        origin_node=NODE_ID,
        visited_nodes=[NODE_ID],
    )
    remaining_deadline_ms = task.deadline_ms - escalation.elapsed_ms
    if remaining_deadline_ms < MIN_REMOTE_BUDGET_MS:
        return await edge_fallback_response(
            task=task,
            edge_result=edge_result,
            started=started,
            trigger_reason=(
                f"边缘推理后剩余 deadline 仅 {max(0, remaining_deadline_ms):.1f}ms，"
                f"不足远端调用的最小预算 {MIN_REMOTE_BUDGET_MS}ms"
            ),
        )

    timeout_seconds = remaining_deadline_ms / 1000
    try:
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(timeout=None) as client:
                result = await client.post(
                    f"{CONTROLLER_URL}/v1/escalate",
                    json=escalation.model_dump(mode="json"),
                )
                result.raise_for_status()
                decision = DecisionResponse.model_validate(result.json())
        if (time.perf_counter() - started) * 1000 > task.deadline_ms:
            raise TimeoutError("controller response arrived after the task deadline")
        return decision
    except (TimeoutError, httpx.HTTPError, OSError):
        return await edge_fallback_response(
            task=task,
            edge_result=edge_result,
            started=started,
            trigger_reason="Controller 不可用或调用超时",
        )
