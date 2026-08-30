from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from common.adaptive_policy import (
    ExecutionCandidate,
    network_snapshot,
    rank_execution_candidates,
)
from common.fallback import conservative_fallback
from common.recorder_client import flush_pending_events, record_event
from common.schemas import (
    ArbitrationRequest,
    ArbitrationResponse,
    DecisionResponse,
    EscalationRequest,
    InferenceResult,
    NodeHeartbeat,
    NodeStatus,
    Route,
)
from services.controller.arbitration import arbitrate
from services.controller.node_registry import NodeRegistry


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await flush_pending_events()


app = FastAPI(title="Cloud-Edge MVP - Controller", version="0.1.0", lifespan=lifespan)

CLOUD_URL = os.getenv("CLOUD_URL", "http://localhost:8003").rstrip("/")
CLOUD_TIMEOUT_MS = int(os.getenv("CLOUD_TIMEOUT_MS", "800"))
PEER_TIMEOUT_MS = int(os.getenv("PEER_TIMEOUT_MS", "400"))
PEER_ENABLED = os.getenv("PEER_ENABLED", "true").lower() == "true"
MIN_REMOTE_BUDGET_MS = 80
NODE_TTL_SECONDS = int(os.getenv("NODE_TTL_SECONDS", "15"))
node_registry = NodeRegistry(ttl_seconds=NODE_TTL_SECONDS)


def _remaining_deadline_ms(request: EscalationRequest, *, started: float) -> float:
    """Return the live controller budget after edge and controller elapsed time."""

    controller_elapsed_ms = (time.perf_counter() - started) * 1000
    return max(0.0, request.task.deadline_ms - request.elapsed_ms - controller_elapsed_ms)


async def _fallback_response(
    request: EscalationRequest,
    *,
    started: float,
    trigger_reason: str,
) -> DecisionResponse:
    prediction, action, confidence, fallback_reason = conservative_fallback(
        request.task, request.edge_result
    )
    decision = DecisionResponse(
        task_id=request.task.task_id,
        route=Route.EDGE_FALLBACK,
        final_prediction=prediction,
        final_action=action,
        final_confidence=confidence,
        decision_reason=f"{trigger_reason}；{fallback_reason}",
        edge_result=request.edge_result,
        cloud_result=None,
        degraded=True,
        total_latency_ms=round(
            request.elapsed_ms + (time.perf_counter() - started) * 1000, 3
        ),
    )
    await record_event(
        task_id=request.task.task_id,
        component="controller",
        event_type="decision",
        route=decision.route.value,
        data=decision.model_dump(mode="json"),
    )
    return decision


def build_execution_plan(
    request: EscalationRequest,
    *,
    remaining_deadline_ms: float,
):
    """Build a DREAM-Route plan from live node and network telemetry."""

    snapshot = network_snapshot(request.task.metadata)
    peer = node_registry.select_peer(
        scene=request.task.scene,
        excluded_node_ids=set(request.visited_nodes) | {request.origin_node},
    )
    candidates = [
        ExecutionCandidate(
            route=Route.EDGE_FALLBACK,
            predicted_latency_ms=1.0,
            expected_accuracy=max(0.50, request.edge_result.confidence * 0.90),
        ),
        ExecutionCandidate(
            route=Route.CLOUD,
            predicted_latency_ms=max(
                float(request.task.metadata.get("cloud_estimated_latency_ms", 350.0)),
                snapshot.rtt_ms + snapshot.jitter_ms,
            ),
            expected_accuracy=float(request.task.metadata.get("cloud_expected_accuracy", 0.96)),
            availability=(
                snapshot.availability
                if bool(request.task.metadata.get("cloud_available", True))
                else 0.0
            ),
            load=float(request.task.metadata.get("cloud_load", 0.20)),
            communication_kb=float(request.task.metadata.get("payload_kb", 64.0)),
            node_id="cloud",
        ),
    ]
    if peer is not None:
        candidates.append(
            ExecutionCandidate(
                route=Route.PEER_EDGE,
                predicted_latency_ms=max(
                    peer.estimated_latency_ms,
                    snapshot.rtt_ms + snapshot.jitter_ms,
                ),
                expected_accuracy=peer.reliability,
                availability=snapshot.availability,
                load=peer.load,
                communication_kb=float(request.task.metadata.get("payload_kb", 64.0)),
                node_id=peer.node_id,
            )
        )
    return rank_execution_candidates(
        request.task,
        candidates,
        remaining_deadline_ms=remaining_deadline_ms,
    )


@app.get("/health")
async def health() -> dict[str, object]:
    healthy_nodes = sum(node.healthy for node in node_registry.list_nodes())
    return {
        "status": "ok",
        "cloud_url": CLOUD_URL,
        "cloud_timeout_ms": CLOUD_TIMEOUT_MS,
        "peer_enabled": PEER_ENABLED,
        "registered_nodes": healthy_nodes,
    }


@app.post("/v1/nodes/heartbeat", response_model=NodeStatus)
async def heartbeat(node: NodeHeartbeat) -> NodeStatus:
    return node_registry.heartbeat(node)


@app.get("/v1/nodes", response_model=list[NodeStatus])
async def list_nodes() -> list[NodeStatus]:
    return node_registry.list_nodes()


@app.post("/v1/arbitrate", response_model=ArbitrationResponse)
async def arbitrate_edges(request: ArbitrationRequest) -> ArbitrationResponse:
    decision = arbitrate(request)
    event_data = decision.model_dump(mode="json")
    ground_truth = request.task.metadata.get("ground_truth_prediction")
    if ground_truth is not None:
        event_data["ground_truth_prediction"] = str(ground_truth)
        event_data["resolution_correct"] = (
            decision.conflict
            and not decision.requires_cloud_review
            and decision.final_prediction == str(ground_truth)
        )
    await record_event(
        task_id=request.task.task_id,
        component="controller",
        event_type="arbitration",
        route=decision.route.value,
        data=event_data,
    )
    return decision


async def try_peer(
    request: EscalationRequest,
    *,
    started: float,
    remaining_deadline_ms: float,
) -> DecisionResponse | None:
    if not PEER_ENABLED or request.hop_count >= 1:
        return None

    remaining_deadline_ms = min(
        remaining_deadline_ms,
        _remaining_deadline_ms(request, started=started),
    )
    if remaining_deadline_ms < MIN_REMOTE_BUDGET_MS:
        return None

    peer = node_registry.select_peer(
        scene=request.task.scene,
        excluded_node_ids=set(request.visited_nodes) | {request.origin_node},
    )
    if peer is None or peer.endpoint_url is None:
        return None
    if peer.estimated_latency_ms + MIN_REMOTE_BUDGET_MS > remaining_deadline_ms:
        return None

    timeout_ms = min(PEER_TIMEOUT_MS, int(remaining_deadline_ms))
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(
                    f"{peer.endpoint_url.rstrip('/')}/v1/infer",
                    json=request.task.model_dump(mode="json"),
                )
                response.raise_for_status()
                peer_result = InferenceResult.model_validate(response.json())
        if _remaining_deadline_ms(request, started=started) <= 0:
            return None
    except (TimeoutError, httpx.HTTPError, OSError):
        return None

    return DecisionResponse(
        task_id=request.task.task_id,
        route=Route.PEER_EDGE,
        final_prediction=peer_result.prediction,
        final_action=peer_result.action,
        final_confidence=peer_result.confidence,
        decision_reason=(
            f"peer={peer.node_id}, estimated_latency={peer.estimated_latency_ms:.1f}ms, "
            f"edge confidence={request.edge_result.confidence:.3f}"
        ),
        edge_result=request.edge_result,
        peer_result=peer_result,
        degraded=False,
        total_latency_ms=round(request.elapsed_ms + (time.perf_counter() - started) * 1000, 3),
    )


@app.post("/v1/escalate", response_model=DecisionResponse)
async def escalate(request: EscalationRequest) -> DecisionResponse:
    started = time.perf_counter()
    remaining_deadline_ms = _remaining_deadline_ms(request, started=started)
    if remaining_deadline_ms < MIN_REMOTE_BUDGET_MS:
        return await _fallback_response(
            request,
            started=started,
            trigger_reason=(
                f"剩余 deadline 仅 {max(0, remaining_deadline_ms):.1f}ms，"
                f"不足远端调用的最小预算 {MIN_REMOTE_BUDGET_MS}ms"
            ),
        )

    execution_plan = build_execution_plan(
        request,
        remaining_deadline_ms=remaining_deadline_ms,
    )
    route_order = [item.candidate.route for item in execution_plan]
    if route_order[0] == Route.EDGE_FALLBACK:
        return await _fallback_response(
            request,
            started=started,
            trigger_reason=(
                "DREAM-Route selected local fallback: "
                f"{execution_plan[0].explanation}"
            ),
        )

    peer_attempted = False
    peer_before_cloud = (
        Route.PEER_EDGE in route_order
        and route_order.index(Route.PEER_EDGE) < route_order.index(Route.CLOUD)
    )
    if peer_before_cloud:
        peer_attempted = True
        peer_decision = await try_peer(
            request,
            started=started,
            remaining_deadline_ms=remaining_deadline_ms,
        )
        if peer_decision is not None:
            await record_event(
                task_id=request.task.task_id,
                component="controller",
                event_type="decision",
                route=peer_decision.route.value,
                data=peer_decision.model_dump(mode="json"),
            )
            return peer_decision

        if route_order.index(Route.EDGE_FALLBACK) < route_order.index(Route.CLOUD):
            return await _fallback_response(
                request,
                started=started,
                trigger_reason="DREAM-Route stopped after peer failure",
            )

    remaining_deadline_ms = _remaining_deadline_ms(request, started=started)
    if remaining_deadline_ms < MIN_REMOTE_BUDGET_MS:
        return await _fallback_response(
            request,
            started=started,
            trigger_reason=(
                f"前序路径消耗预算后仅剩 {remaining_deadline_ms:.1f}ms，"
                f"不足云端调用的最小预算 {MIN_REMOTE_BUDGET_MS}ms"
            ),
        )

    remaining_ms = min(CLOUD_TIMEOUT_MS, max(1, int(remaining_deadline_ms)))

    try:
        async with asyncio.timeout(remaining_ms / 1000):
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(
                    f"{CLOUD_URL}/v1/infer",
                    json=request.task.model_dump(mode="json"),
                )
                response.raise_for_status()
                cloud_result = InferenceResult.model_validate(response.json())
        if _remaining_deadline_ms(request, started=started) <= 0:
            raise TimeoutError("cloud response arrived after the task deadline")

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
    except (TimeoutError, httpx.HTTPError, OSError):
        if (
            Route.PEER_EDGE in route_order
            and not peer_attempted
            and route_order.index(Route.PEER_EDGE) < route_order.index(Route.EDGE_FALLBACK)
        ):
            peer_decision = await try_peer(
                request,
                started=started,
                remaining_deadline_ms=_remaining_deadline_ms(request, started=started),
            )
            if peer_decision is not None:
                await record_event(
                    task_id=request.task.task_id,
                    component="controller",
                    event_type="decision",
                    route=peer_decision.route.value,
                    data=peer_decision.model_dump(mode="json"),
                )
                return peer_decision
        return await _fallback_response(
            request,
            started=started,
            trigger_reason="所有可行远端路径均不可用或超时",
        )

    await record_event(
        task_id=request.task.task_id,
        component="controller",
        event_type="decision",
        route=decision.route.value,
        data=decision.model_dump(mode="json"),
    )
    return decision
