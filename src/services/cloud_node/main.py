from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI

from common.recorder_client import record_event
from common.schemas import InferenceResult, TaskRequest
from services.cloud_node.core import infer_industrial, infer_traffic

app = FastAPI(title="Cloud-Edge MVP - Cloud Node", version="0.1.0")
DELAY_MS = int(os.getenv("CLOUD_INFERENCE_DELAY_MS", "350"))


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "model": "cloud-fusion-model-v0.1", "delay_ms": DELAY_MS}


@app.post("/v1/infer", response_model=InferenceResult)
async def infer(task: TaskRequest) -> InferenceResult:
    started = time.perf_counter()
    await asyncio.sleep(DELAY_MS / 1000)

    if task.scene == "traffic":
        prediction, action, reason = infer_traffic(task.payload)
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
        event_type="inference",
        route="CLOUD",
        data=result.model_dump(mode="json"),
    )
    return result
