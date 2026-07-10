from __future__ import annotations

import os
import time

import httpx
from fastapi import FastAPI, HTTPException

from common.recorder_client import record_event
from common.schemas import DecisionResponse, EscalationRequest, Route, TaskRequest
from services.edge_node.core import choose_local_route, infer_locally, safety_action_for

app = FastAPI(title="Cloud-Edge MVP - Edge Node", version="0.1.0")

NODE_ID = os.getenv("NODE_ID", "edge-a")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8002").rstrip("/")
LOCAL_THRESHOLD = float(os.getenv("LOCAL_CONFIDENCE_THRESHOLD", "0.80"))


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "node_id": NODE_ID, "threshold": LOCAL_THRESHOLD}


@app.post("/v1/infer")
async def infer(task: TaskRequest):
    return infer_locally(task, NODE_ID)


@app.post("/v1/tasks", response_model=DecisionResponse)
async def process_task(task: TaskRequest) -> DecisionResponse:
    started = time.perf_counter()
    edge_result = infer_locally(task, NODE_ID)
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
    timeout_seconds = max(0.1, task.deadline_ms / 1000)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            result = await client.post(
                f"{CONTROLLER_URL}/v1/escalate",
                json=escalation.model_dump(mode="json"),
            )
            result.raise_for_status()
            return DecisionResponse.model_validate(result.json())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"controller unavailable: {exc}") from exc
