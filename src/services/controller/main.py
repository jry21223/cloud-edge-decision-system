from __future__ import annotations

import os
import time

import httpx
from fastapi import FastAPI

from common.recorder_client import record_event
from common.schemas import DecisionResponse, EscalationRequest, InferenceResult, Route
from services.controller.core import conservative_fallback

app = FastAPI(title="Cloud-Edge MVP - Controller", version="0.1.0")

CLOUD_URL = os.getenv("CLOUD_URL", "http://localhost:8003").rstrip("/")
CLOUD_TIMEOUT_MS = int(os.getenv("CLOUD_TIMEOUT_MS", "800"))


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "cloud_url": CLOUD_URL, "cloud_timeout_ms": CLOUD_TIMEOUT_MS}


@app.post("/v1/escalate", response_model=DecisionResponse)
async def escalate(request: EscalationRequest) -> DecisionResponse:
    started = time.perf_counter()
    remaining_ms = max(80, min(CLOUD_TIMEOUT_MS, request.task.deadline_ms - int(request.elapsed_ms)))

    try:
        async with httpx.AsyncClient(timeout=remaining_ms / 1000) as client:
            response = await client.post(
                f"{CLOUD_URL}/v1/infer",
                json=request.task.model_dump(mode="json"),
            )
            response.raise_for_status()
            cloud_result = InferenceResult.model_validate(response.json())

        decision = DecisionResponse(
            task_id=request.task.task_id,
            route=Route.CLOUD,
            final_prediction=cloud_result.prediction,
            final_action=cloud_result.action,
            final_confidence=cloud_result.confidence,
            decision_reason=(
                f"edge confidence={request.edge_result.confidence:.3f} 不足；"
                f"云端在 {remaining_ms}ms 预算内返回"
            ),
            edge_result=request.edge_result,
            cloud_result=cloud_result,
            degraded=False,
            total_latency_ms=round(request.elapsed_ms + (time.perf_counter() - started) * 1000, 3),
        )
    except (httpx.HTTPError, OSError):
        prediction, action, confidence, reason = conservative_fallback(request.task, request.edge_result)
        decision = DecisionResponse(
            task_id=request.task.task_id,
            route=Route.EDGE_FALLBACK,
            final_prediction=prediction,
            final_action=action,
            final_confidence=confidence,
            decision_reason=reason,
            edge_result=request.edge_result,
            cloud_result=None,
            degraded=True,
            total_latency_ms=round(request.elapsed_ms + (time.perf_counter() - started) * 1000, 3),
        )

    await record_event(
        task_id=request.task.task_id,
        component="controller",
        event_type="decision",
        route=decision.route.value,
        data=decision.model_dump(mode="json"),
    )
    return decision
