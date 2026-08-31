from __future__ import annotations

import asyncio
import hashlib
import os
import time
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, HTTPException

from common.fallback import conservative_fallback
from common.recorder_client import flush_pending_events, record_event
from common.schemas import (
    ArbitrationRequest,
    ArbitrationResponse,
    DecisionResponse,
    EdgeProposal,
    EscalationRequest,
    InferenceResult,
    NodeHeartbeat,
    Route,
    RoutingCandidate,
    RoutingDecision,
    TaskRequest,
    UploadMode,
)
from common.telemetry import RuntimeTelemetry
from common.vision import crop_roi
from services.edge_node.core import choose_local_route, infer_locally, safety_action_for
from services.edge_node.llm_adapter import warm_llm_if_enabled
from services.edge_node.outbox import (
    EdgeStateStore,
    RequestConflictError,
    flush_outbox_once,
)

NODE_ID = os.getenv("NODE_ID", "edge-a")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8002").rstrip("/")
NODE_ENDPOINT_URL = os.getenv("NODE_ENDPOINT_URL", f"http://{NODE_ID}:8000").rstrip("/")
LOCAL_THRESHOLD = float(os.getenv("LOCAL_CONFIDENCE_THRESHOLD", "0.80"))
MIN_REMOTE_BUDGET_MS = 80
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "2"))
OUTBOX_FLUSH_INTERVAL_SECONDS = float(os.getenv("OUTBOX_FLUSH_INTERVAL_SECONDS", "2"))
runtime_telemetry = RuntimeTelemetry(capacity=int(os.getenv("EDGE_MAX_CONCURRENCY", "4")))
_TASK_LOCK_STRIPES = tuple(asyncio.Lock() for _ in range(256))


@lru_cache(maxsize=1)
def state_store() -> EdgeStateStore:
    return EdgeStateStore(os.getenv("EDGE_STATE_DB", ":memory:"))


def heartbeat_payload() -> NodeHeartbeat:
    snapshot = runtime_telemetry.snapshot()
    return NodeHeartbeat(
        node_id=NODE_ID,
        endpoint_url=NODE_ENDPOINT_URL,
        load=snapshot.load,
        queue_depth=snapshot.queue_depth,
        estimated_latency_ms=snapshot.estimated_latency_ms,
        model_version=os.getenv("NODE_MODEL_VERSION", "edge-rule+vision-baseline-v1"),
        cpu_utilization=snapshot.cpu_utilization,
        memory_utilization=snapshot.memory_utilization,
        process_rss_mb=snapshot.process_rss_mb,
        gpu_utilization=snapshot.gpu_utilization,
        gpu_memory_used_mb=snapshot.gpu_memory_used_mb,
        rtt_ms=snapshot.rtt_ms,
        bandwidth_mbps=snapshot.bandwidth_mbps,
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


async def outbox_loop() -> None:
    while True:
        await flush_outbox_once(state_store())
        await asyncio.sleep(OUTBOX_FLUSH_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("EDGE_LLM_WARM_ON_START", "false").lower() == "true":
        await asyncio.to_thread(warm_llm_if_enabled)
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    outbox_task = asyncio.create_task(outbox_loop())
    try:
        yield
    finally:
        heartbeat_task.cancel()
        outbox_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        with suppress(asyncio.CancelledError):
            await outbox_task
        await flush_outbox_once(state_store())
        await flush_pending_events()


app = FastAPI(title="Cloud-Edge MVP - Edge Node", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    snapshot = await asyncio.to_thread(runtime_telemetry.snapshot)
    return {
        "status": "ok",
        "node_id": NODE_ID,
        "threshold": LOCAL_THRESHOLD,
        "inference_backend": os.getenv("EDGE_INFERENCE_BACKEND", "rule"),
        "telemetry": snapshot.__dict__,
        "outbox": await asyncio.to_thread(state_store().counts),
    }


@app.post("/v1/infer", response_model=InferenceResult)
async def infer(
    task: TaskRequest,
    _idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> InferenceResult:
    started = time.perf_counter()
    await runtime_telemetry.acquire_task()
    try:
        return await asyncio.to_thread(infer_locally, task, NODE_ID)
    finally:
        runtime_telemetry.release_task(
            latency_ms=(time.perf_counter() - started) * 1000
        )


async def edge_fallback_response(
    *,
    task: TaskRequest,
    edge_result: InferenceResult,
    started: float,
    trigger_reason: str,
    outbox_id: str | None = None,
    upload_mode: UploadMode | None = None,
) -> DecisionResponse:
    prediction, action, confidence, fallback_reason = conservative_fallback(task, edge_result)
    total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
    response = DecisionResponse(
        task_id=task.task_id,
        trace_id=task.trace_id,
        association_id=task.task_id,
        route=Route.EDGE_FALLBACK,
        final_prediction=prediction,
        final_action=action,
        final_confidence=confidence,
        decision_reason=f"{trigger_reason}；{fallback_reason}",
        edge_result=edge_result,
        upload_mode=upload_mode,
        outbox_id=outbox_id,
        degraded=True,
        total_latency_ms=total_latency_ms,
        deadline_met=total_latency_ms <= task.deadline_ms,
    )
    await record_event(
        task_id=task.task_id,
        component=NODE_ID,
        event_type="decision",
        route=response.route.value,
        data={
            **response.model_dump(mode="json"),
            "workpiece_id": task.workpiece_id,
            "station_id": task.station_id,
            "batch_id": task.batch_id,
            "image_sha256": task.image.sha256 if task.image else None,
        },
    )
    return response


def _remaining_deadline_ms(task: TaskRequest, *, started: float) -> float:
    return max(0.0, task.deadline_ms - (time.perf_counter() - started) * 1000)


def _prepare_upload_task(
    task: TaskRequest,
    edge_result: InferenceResult,
    upload_mode: UploadMode,
) -> TaskRequest:
    metadata = {**task.metadata, "upload_mode": upload_mode.value}
    if task.image is None or upload_mode == UploadMode.METADATA:
        return task.control_plane_copy().model_copy(update={"metadata": metadata})
    if upload_mode == UploadMode.ROI:
        if not edge_result.detections:
            raise ValueError("ROI upload requires a localized detection")
        roi_image = crop_roi(
            task.image,
            edge_result.detections[0].bbox,
            margin_ratio=float(task.metadata.get("roi_margin_ratio", 0.15)),
        )
        roi_image = roi_image.model_copy(update={"local_ref": None})
        metadata["source_image_sha256"] = task.image.sha256
        return task.model_copy(update={"image": roi_image, "metadata": metadata})
    raw_image = task.image.model_copy(update={"local_ref": None})
    return task.model_copy(update={"image": raw_image, "metadata": metadata})


def _ordered_remote_candidates(plan: RoutingDecision) -> list[RoutingCandidate]:
    selected_key = (plan.route, plan.target_node, plan.target_endpoint)
    candidates = [
        item
        for item in plan.candidates
        if item.feasible and item.route in {Route.PEER_EDGE, Route.CLOUD}
    ]
    return sorted(
        candidates,
        key=lambda item: (
            (item.route, item.target_node, item.target_endpoint) != selected_key,
            item.score,
            item.route.value,
            item.target_node or "",
        ),
    )


async def _call_remote_candidate(
    task: TaskRequest,
    edge_result: InferenceResult,
    candidate: RoutingCandidate,
    *,
    started: float,
) -> tuple[InferenceResult, int] | None:
    if candidate.target_endpoint is None:
        return None
    remaining_ms = _remaining_deadline_ms(task, started=started)
    timeout_ms = min(candidate.timeout_ms, max(0, int(remaining_ms)))
    if timeout_ms < 1:
        return None
    try:
        upload_task = _prepare_upload_task(task, edge_result, candidate.upload_mode)
    except ValueError:
        return None
    request_body = upload_task.model_dump_json().encode("utf-8")
    idempotency_key = hashlib.sha256(
        (
            f"{task.task_id}:{candidate.target_node}:"
            f"{upload_task.image.sha256 if upload_task.image else 'metadata'}"
        ).encode()
    ).hexdigest()
    call_started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(
                    f"{candidate.target_endpoint.rstrip('/')}/v1/infer",
                    content=request_body,
                    headers={
                        "content-type": "application/json",
                        "Idempotency-Key": idempotency_key,
                    },
                )
                response.raise_for_status()
                response_bytes = await response.aread()
                result = InferenceResult.model_validate_json(response_bytes)
        elapsed_ms = (time.perf_counter() - call_started) * 1000
        runtime_telemetry.observe_network(
            elapsed_ms=elapsed_ms,
            transferred_bytes=len(request_body) + len(response_bytes),
            success=True,
        )
        if _remaining_deadline_ms(task, started=started) <= 0:
            return None
        return result, upload_task.image.byte_size if upload_task.image else 0
    except (TimeoutError, httpx.HTTPError, OSError, ValueError):
        runtime_telemetry.observe_network(
            elapsed_ms=(time.perf_counter() - call_started) * 1000,
            success=False,
        )
        return None


async def _request_fusion(
    task: TaskRequest,
    edge_result: InferenceResult,
    peer_results: list[InferenceResult],
    *,
    started: float,
) -> ArbitrationResponse | None:
    # P0 fusion is task-scoped multi-Peer evidence. Cross-station workpiece
    # accumulation needs a separate observation-epoch contract and is not
    # inferred from workpiece_id alone.
    association_id = task.task_id

    def proposal(result: InferenceResult) -> EdgeProposal:
        proposal_id = hashlib.sha256(
            f"{association_id}:{task.task_id}:{result.node_id}:0".encode()
        ).hexdigest()
        return EdgeProposal(
            proposal_id=proposal_id,
            node_id=result.node_id,
            result=result,
        )

    proposals = [proposal(edge_result)] + [proposal(result) for result in peer_results]
    if len(proposals) < 2:
        return None
    request = ArbitrationRequest(
        task=task.control_plane_copy(),
        proposals=proposals,
        association_id=association_id,
    )
    remaining_ms = _remaining_deadline_ms(task, started=started)
    if remaining_ms < 1:
        return None
    try:
        async with asyncio.timeout(remaining_ms / 1000):
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(
                    f"{CONTROLLER_URL}/v1/arbitrate",
                    json=request.model_dump(mode="json"),
                )
                response.raise_for_status()
                return ArbitrationResponse.model_validate(response.json())
    except (TimeoutError, httpx.HTTPError, OSError, ValueError):
        return None


async def _enqueue_late_review(
    task: TaskRequest,
    edge_result: InferenceResult,
    candidates: list[RoutingCandidate],
) -> tuple[str | None, UploadMode | None]:
    cloud = next(
        (
            item
            for item in candidates
            if item.route == Route.CLOUD and item.target_endpoint is not None
        ),
        None,
    )
    if cloud is None:
        return None, None
    try:
        upload_task = _prepare_upload_task(task, edge_result, cloud.upload_mode)
    except ValueError:
        return None, None
    image_key = upload_task.image.sha256 if upload_task.image else "metadata"
    idempotency_key = f"late-review:{task.task_id}:{image_key}"
    item_id = await asyncio.to_thread(
        state_store().enqueue_review,
        task_id=task.task_id,
        idempotency_key=idempotency_key,
        target_url=cloud.target_endpoint,
        payload=upload_task.model_dump(mode="json"),
    )
    return item_id, cloud.upload_mode


async def _process_vision_task(
    task: TaskRequest,
    edge_result: InferenceResult,
    *,
    started: float,
) -> DecisionResponse:
    escalation = EscalationRequest(
        task=task.control_plane_copy(),
        edge_result=edge_result,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        origin_node=NODE_ID,
        visited_nodes=[NODE_ID],
    )
    remaining_deadline_ms = _remaining_deadline_ms(task, started=started)
    if remaining_deadline_ms < MIN_REMOTE_BUDGET_MS:
        return await edge_fallback_response(
            task=task,
            edge_result=edge_result,
            started=started,
            trigger_reason="视觉边缘推理后远端预算不足",
        )

    controller_started = time.perf_counter()
    try:
        async with asyncio.timeout(remaining_deadline_ms / 1000):
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(
                    f"{CONTROLLER_URL}/v1/routes/decide",
                    json=escalation.model_dump(mode="json"),
                )
                response.raise_for_status()
                response_payload = await response.aread()
                plan = RoutingDecision.model_validate_json(response_payload)
        runtime_telemetry.observe_network(
            elapsed_ms=(time.perf_counter() - controller_started) * 1000,
            transferred_bytes=len(response_payload),
            success=True,
        )
    except (TimeoutError, httpx.HTTPError, OSError, ValueError):
        runtime_telemetry.observe_network(
            elapsed_ms=(time.perf_counter() - controller_started) * 1000,
            success=False,
        )
        return await edge_fallback_response(
            task=task,
            edge_result=edge_result,
            started=started,
            trigger_reason="Controller 控制平面不可用或超时",
        )

    remote_candidates = _ordered_remote_candidates(plan)
    if plan.route == Route.EDGE_FALLBACK or not remote_candidates:
        return await edge_fallback_response(
            task=task,
            edge_result=edge_result,
            started=started,
            trigger_reason=f"DREAM-Route 选择本地回退：{plan.decision_reason}",
        )

    # Vision tasks use the multi-Peer DREAM-Fuse path by default. Set
    # ``fusion_required=false`` only for an explicit single-target experiment.
    fusion_required = bool(task.metadata.get("fusion_required", True))
    if fusion_required:
        peer_candidates = [item for item in remote_candidates if item.route == Route.PEER_EDGE]
        peer_calls = [
            _call_remote_candidate(task, edge_result, item, started=started)
            for item in peer_candidates
        ]
        peer_attempts = await asyncio.gather(*peer_calls) if peer_calls else []
        peer_results = [item[0] for item in peer_attempts if item is not None]
        arbitration = await _request_fusion(
            task,
            edge_result,
            peer_results,
            started=started,
        )
        if arbitration is not None and not arbitration.requires_cloud_review:
            chosen_peer = next(
                (item for item in peer_results if item.node_id == arbitration.chosen_node),
                None,
            )
            total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            decision = DecisionResponse(
                task_id=task.task_id,
                trace_id=task.trace_id,
                association_id=task.task_id,
                route=arbitration.route,
                final_prediction=arbitration.final_prediction,
                final_action=arbitration.final_action,
                final_confidence=arbitration.final_confidence,
                decision_reason=arbitration.resolution_reason,
                edge_result=edge_result,
                peer_result=chosen_peer,
                arbitration=arbitration,
                degraded=False,
                total_latency_ms=total_latency_ms,
                deadline_met=total_latency_ms <= task.deadline_ms,
            )
            await record_event(
                task_id=task.task_id,
                component=NODE_ID,
                event_type="decision",
                route=decision.route.value,
                data=decision.model_dump(mode="json"),
            )
            return decision
        remote_candidates = [item for item in remote_candidates if item.route == Route.CLOUD]

    for candidate in remote_candidates:
        attempt = await _call_remote_candidate(task, edge_result, candidate, started=started)
        if attempt is None:
            continue
        remote_result, uploaded_bytes = attempt
        total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
        decision = DecisionResponse(
            task_id=task.task_id,
            trace_id=task.trace_id,
            association_id=task.task_id,
            route=candidate.route,
            final_prediction=remote_result.prediction,
            final_action=remote_result.action,
            final_confidence=remote_result.confidence,
            decision_reason=(
                f"Edge 按 Controller 计划直传 {candidate.upload_mode.value} 至 "
                f"{candidate.target_node}"
            ),
            edge_result=edge_result,
            cloud_result=remote_result if candidate.route == Route.CLOUD else None,
            peer_result=remote_result if candidate.route == Route.PEER_EDGE else None,
            upload_mode=candidate.upload_mode,
            uploaded_bytes=uploaded_bytes,
            degraded=False,
            total_latency_ms=total_latency_ms,
            deadline_met=total_latency_ms <= task.deadline_ms,
        )
        await record_event(
            task_id=task.task_id,
            component=NODE_ID,
            event_type="decision",
            route=decision.route.value,
            data=decision.model_dump(mode="json"),
        )
        return decision

    outbox_id, upload_mode = await _enqueue_late_review(task, edge_result, plan.candidates)
    return await edge_fallback_response(
        task=task,
        edge_result=edge_result,
        started=started,
        trigger_reason="所有视觉远端路径均不可用或超时；现场动作已冻结",
        outbox_id=outbox_id,
        upload_mode=upload_mode,
    )


async def _process_legacy_task(task: TaskRequest, *, started: float) -> DecisionResponse:
    edge_result = await asyncio.to_thread(infer_locally, task, NODE_ID)
    local_route = choose_local_route(task, edge_result, LOCAL_THRESHOLD)

    if local_route is not None:
        action = safety_action_for(task.scene) if local_route == Route.EDGE_SAFETY else edge_result.action
        reason = (
            "高风险任务由边缘端立即执行安全动作，不等待远端"
            if local_route == Route.EDGE_SAFETY
            else f"confidence={edge_result.confidence:.3f} ≥ threshold={LOCAL_THRESHOLD:.3f}"
        )
        total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
        response = DecisionResponse(
            task_id=task.task_id,
            trace_id=task.trace_id,
            association_id=task.task_id,
            route=local_route,
            final_prediction=edge_result.prediction,
            final_action=action,
            final_confidence=edge_result.confidence,
            decision_reason=reason,
            edge_result=edge_result,
            degraded=False,
            total_latency_ms=total_latency_ms,
            deadline_met=total_latency_ms <= task.deadline_ms,
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


async def _process_task_inner(task: TaskRequest) -> DecisionResponse:
    started = time.perf_counter()
    await runtime_telemetry.acquire_task()
    request_payload = task.model_dump(mode="json")
    try:
        if task.image is not None:
            try:
                cached = await asyncio.to_thread(
                    state_store().cached_response,
                    task_id=task.task_id,
                    request_payload=request_payload,
                )
            except RequestConflictError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            if cached is not None:
                return DecisionResponse.model_validate(cached)

            edge_result = await asyncio.to_thread(infer_locally, task, NODE_ID)
            local_route = choose_local_route(task, edge_result, LOCAL_THRESHOLD)
            if local_route is not None:
                total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
                response = DecisionResponse(
                    task_id=task.task_id,
                    trace_id=task.trace_id,
                    association_id=task.task_id,
                    route=local_route,
                    final_prediction=edge_result.prediction,
                    final_action=(
                        safety_action_for(task.scene)
                        if local_route == Route.EDGE_SAFETY
                        else edge_result.action
                    ),
                    final_confidence=edge_result.confidence,
                    decision_reason="视觉质量门控/本地基线满足安全终结条件",
                    edge_result=edge_result,
                    degraded=False,
                    total_latency_ms=total_latency_ms,
                    deadline_met=total_latency_ms <= task.deadline_ms,
                )
                await record_event(
                    task_id=task.task_id,
                    component=NODE_ID,
                    event_type="decision",
                    route=response.route.value,
                    data=response.model_dump(mode="json"),
                )
            else:
                response = await _process_vision_task(task, edge_result, started=started)

            try:
                await asyncio.to_thread(
                    state_store().commit_action,
                    task_id=task.task_id,
                    request_payload=request_payload,
                    action=response.final_action,
                    response_payload=response.model_dump(mode="json"),
                )
            except RequestConflictError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return response

        return await _process_legacy_task(task, started=started)
    finally:
        runtime_telemetry.release_task(
            latency_ms=(time.perf_counter() - started) * 1000
        )


@app.post("/v1/tasks", response_model=DecisionResponse)
async def process_task(task: TaskRequest) -> DecisionResponse:
    if task.image is None:
        return await _process_task_inner(task)

    # A deterministic striped lock prevents concurrent retries of the same
    # visual task from performing remote fan-out before the durable result is
    # committed. SQLite preserves the result across process restarts.
    digest = hashlib.sha256(task.task_id.encode("utf-8")).digest()
    lock = _TASK_LOCK_STRIPES[int.from_bytes(digest[:2], "big") % len(_TASK_LOCK_STRIPES)]
    async with lock:
        return await _process_task_inner(task)
