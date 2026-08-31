from __future__ import annotations

import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager
from functools import lru_cache

import httpx
from fastapi import FastAPI, Header, HTTPException

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
    RoutingCandidate,
    RoutingDecision,
    UploadMode,
)
from services.controller.arbitration import arbitrate
from services.controller.fusion_store import FusionConflictError, FusionStore
from services.controller.node_registry import NodeRegistry


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await flush_pending_events()


app = FastAPI(title="Cloud-Edge MVP - Controller", version="0.1.0", lifespan=lifespan)

CLOUD_URL = os.getenv("CLOUD_URL", "http://localhost:8003").rstrip("/")
CLOUD_TIMEOUT_MS = int(os.getenv("CLOUD_TIMEOUT_MS", "800"))
CLOUD_BASE_SERVICE_MS = float(os.getenv("CLOUD_BASE_SERVICE_MS", "120"))
PEER_TIMEOUT_MS = int(os.getenv("PEER_TIMEOUT_MS", "400"))
PEER_ENABLED = os.getenv("PEER_ENABLED", "true").lower() == "true"
MIN_REMOTE_BUDGET_MS = 80
NODE_TTL_SECONDS = int(os.getenv("NODE_TTL_SECONDS", "15"))
MAX_ROUTE_PEERS = int(os.getenv("MAX_ROUTE_PEERS", "3"))
node_registry = NodeRegistry(ttl_seconds=NODE_TTL_SECONDS)
NODE_REGISTRATION_TOKEN = os.getenv("NODE_REGISTRATION_TOKEN", "")


def _trusted_node_endpoints() -> dict[str, str | None]:
    raw = os.getenv(
        "TRUSTED_NODE_ENDPOINTS",
        "edge-a=http://edge-a:8000,edge-b=http://edge-b:8000,cloud-node=",
    )
    trusted: dict[str, str | None] = {}
    for entry in raw.split(","):
        if not entry.strip() or "=" not in entry:
            continue
        node_id, endpoint = entry.split("=", 1)
        trusted[node_id.strip()] = endpoint.strip().rstrip("/") or None
    return trusted


@lru_cache(maxsize=1)
def fusion_store() -> FusionStore:
    return FusionStore(os.getenv("FUSION_STATE_DB", ":memory:"))


def _remaining_deadline_ms(request: EscalationRequest, *, started: float) -> float:
    """Return the live controller budget after edge and controller elapsed time."""

    controller_elapsed_ms = (time.perf_counter() - started) * 1000
    return max(0.0, request.task.deadline_ms - request.elapsed_ms - controller_elapsed_ms)


async def _fallback_response(
    request: EscalationRequest,
    *,
    started: float,
    trigger_reason: str,
    attempted_routes: list[Route] | None = None,
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
        attempted_routes=attempted_routes or [],
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
    origin = node_registry.get_node(request.origin_node)
    observed_rtt_ms = (
        origin.rtt_ms if origin is not None and origin.rtt_ms is not None else snapshot.rtt_ms
    )
    observed_bandwidth_mbps = (
        origin.bandwidth_mbps
        if origin is not None and origin.bandwidth_mbps is not None
        else float(request.task.metadata.get("bandwidth_mbps", 20.0))
    )
    payload_kb = (
        request.task.image.byte_size / 1024
        if request.task.image is not None
        else float(request.task.metadata.get("payload_kb", 64.0))
    )
    cloud_status = node_registry.get_node("cloud-node")
    cloud_service_ms = (
        cloud_status.estimated_latency_ms
        if cloud_status is not None and cloud_status.healthy
        else CLOUD_BASE_SERVICE_MS
    )
    cloud_load = (
        max(
            cloud_status.load,
            cloud_status.cpu_utilization or 0.0,
            cloud_status.gpu_utilization or 0.0,
        )
        if cloud_status is not None and cloud_status.healthy
        else float(request.task.metadata.get("cloud_load", 0.20))
    )
    cloud_available = bool(request.task.metadata.get("cloud_available", True)) and (
        cloud_status is None or cloud_status.healthy
    )
    upload_ms = payload_kb * 8.192 / max(observed_bandwidth_mbps, 0.1)
    peers = node_registry.select_peers(
        scene=request.task.scene,
        excluded_node_ids=set(request.visited_nodes) | {request.origin_node},
        limit=MAX_ROUTE_PEERS,
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
                (
                    cloud_service_ms + observed_rtt_ms + snapshot.jitter_ms + upload_ms
                    if request.task.image is not None
                    else float(
                        request.task.metadata.get(
                            "cloud_estimated_latency_ms",
                            cloud_service_ms + observed_rtt_ms + snapshot.jitter_ms + upload_ms,
                        )
                    )
                ),
                observed_rtt_ms + snapshot.jitter_ms + upload_ms,
            ),
            expected_accuracy=float(
                request.task.metadata.get(
                    "cloud_expected_accuracy",
                    cloud_status.reliability if cloud_status is not None else 0.96,
                )
            ),
            availability=(
                snapshot.availability
                if cloud_available
                else 0.0
            ),
            load=cloud_load,
            communication_kb=payload_kb,
            node_id="cloud",
        ),
    ]
    for peer in peers:
        candidates.append(
            ExecutionCandidate(
                route=Route.PEER_EDGE,
                predicted_latency_ms=max(
                    peer.estimated_latency_ms + upload_ms,
                    (peer.rtt_ms or observed_rtt_ms) + snapshot.jitter_ms + upload_ms,
                ),
                expected_accuracy=peer.reliability,
                availability=snapshot.availability,
                load=max(peer.load, peer.cpu_utilization or 0.0, peer.gpu_utilization or 0.0),
                communication_kb=payload_kb,
                node_id=peer.node_id,
            )
        )
    return rank_execution_candidates(
        request.task,
        candidates,
        remaining_deadline_ms=remaining_deadline_ms,
    )


def _vision_upload_mode(request: EscalationRequest, route: Route) -> UploadMode:
    if request.task.image is None or route == Route.EDGE_FALLBACK:
        return UploadMode.METADATA
    if request.edge_result.detections:
        return UploadMode.ROI
    if bool(request.task.metadata.get("allow_raw_upload", False)):
        return UploadMode.RAW
    return UploadMode.METADATA


def create_routing_decision(
    request: EscalationRequest,
    *,
    remaining_deadline_ms: float,
) -> RoutingDecision:
    """Produce a byte-free control-plane decision; this function performs no I/O."""

    if request.task.image is not None and (
        request.task.image.data_base64 is not None or request.task.image.local_ref is not None
    ):
        raise ValueError(
            "Controller routing requests must not contain image bytes or Edge-local references"
        )

    ranked = build_execution_plan(
        request,
        remaining_deadline_ms=remaining_deadline_ms,
    )
    peers = {node.node_id: node for node in node_registry.list_nodes()}
    candidates: list[RoutingCandidate] = []
    rejected_reasons: list[str] = []
    for item in ranked:
        candidate = item.candidate
        target_node = candidate.node_id
        target_endpoint: str | None = None
        if candidate.route == Route.CLOUD:
            target_node = "cloud"
            target_endpoint = CLOUD_URL
        elif candidate.route == Route.PEER_EDGE and candidate.node_id in peers:
            target_endpoint = peers[candidate.node_id].endpoint_url

        upload_mode = _vision_upload_mode(request, candidate.route)
        feasible = item.deadline_feasible
        explanation = item.explanation
        if (
            request.task.image is not None
            and candidate.route in {Route.CLOUD, Route.PEER_EDGE}
            and upload_mode == UploadMode.METADATA
        ):
            feasible = False
            explanation = f"{explanation}; no permitted ROI/RAW artifact"
        if candidate.route in {Route.CLOUD, Route.PEER_EDGE} and not target_endpoint:
            feasible = False
            explanation = f"{explanation}; target endpoint unavailable"

        timeout_ms = 0
        if feasible and candidate.route != Route.EDGE_FALLBACK:
            route_cap = CLOUD_TIMEOUT_MS if candidate.route == Route.CLOUD else PEER_TIMEOUT_MS
            timeout_ms = max(1, min(route_cap, int(remaining_deadline_ms)))
        routing_candidate = RoutingCandidate(
            route=candidate.route,
            target_node=target_node,
            target_endpoint=target_endpoint,
            upload_mode=upload_mode,
            timeout_ms=timeout_ms,
            estimated_finish_ms=max(0.0, candidate.predicted_latency_ms),
            score=item.score,
            feasible=feasible,
            explanation=explanation,
        )
        candidates.append(routing_candidate)
        if not feasible:
            key = f"{candidate.route.value}@{target_node or '-'}"
            rejected_reasons.append(f"{key}: {explanation}")

    feasible_candidates = [item for item in candidates if item.feasible]
    if not feasible_candidates:
        raise RuntimeError("routing plan contains no feasible candidate")
    selected = feasible_candidates[0]
    snapshot = network_snapshot(request.task.metadata)
    candidate_scores = {
        f"{item.route.value}@{item.target_node or '-'}": item.score for item in candidates
    }
    return RoutingDecision(
        task_id=request.task.task_id,
        trace_id=request.task.trace_id,
        route=selected.route,
        target_node=selected.target_node,
        target_endpoint=selected.target_endpoint,
        upload_mode=selected.upload_mode,
        timeout_ms=selected.timeout_ms,
        estimated_finish_ms=selected.estimated_finish_ms,
        decision_reason=selected.explanation,
        candidate_scores=candidate_scores,
        rejected_reasons=rejected_reasons,
        candidates=candidates,
        network_snapshot={
            "availability": snapshot.availability,
            "rtt_ms": snapshot.rtt_ms,
            "jitter_ms": snapshot.jitter_ms,
            "packet_loss": snapshot.packet_loss,
            "remaining_deadline_ms": max(0.0, remaining_deadline_ms),
        },
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
async def heartbeat(
    node: NodeHeartbeat,
    registration_token: str | None = Header(
        default=None,
        alias="X-Node-Registration-Token",
    ),
) -> NodeStatus:
    trusted = _trusted_node_endpoints()
    if node.node_id not in trusted:
        raise HTTPException(status_code=403, detail="node_id is not in the trusted allowlist")
    expected_endpoint = trusted[node.node_id]
    observed_endpoint = node.endpoint_url.rstrip("/") if node.endpoint_url else None
    if observed_endpoint != expected_endpoint:
        raise HTTPException(status_code=403, detail="node endpoint does not match trusted mapping")
    if NODE_REGISTRATION_TOKEN and not (
        registration_token
        and hmac.compare_digest(registration_token, NODE_REGISTRATION_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="invalid node registration token")
    status = node_registry.heartbeat(node)
    await record_event(
        task_id=f"heartbeat:{node.node_id}",
        component="controller",
        event_type="telemetry_snapshot",
        data=status.model_dump(mode="json"),
    )
    return status


@app.get("/v1/nodes", response_model=list[NodeStatus])
async def list_nodes() -> list[NodeStatus]:
    return node_registry.list_nodes()


@app.post("/v1/routes/decide", response_model=RoutingDecision)
async def decide_route(request: EscalationRequest) -> RoutingDecision:
    """Return a control-plane plan without forwarding image bytes."""

    started = time.perf_counter()
    try:
        decision = create_routing_decision(
            request,
            remaining_deadline_ms=_remaining_deadline_ms(request, started=started),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await record_event(
        task_id=request.task.task_id,
        component="controller",
        event_type="routing_plan",
        route=decision.route.value,
        data={
            **decision.model_dump(mode="json"),
            "workpiece_id": request.task.workpiece_id,
            "station_id": request.task.station_id,
            "batch_id": request.task.batch_id,
            "image_sha256": request.task.image.sha256 if request.task.image else None,
            "controller_received_image_bytes": False,
        },
    )
    return decision


@app.post("/v1/arbitrate", response_model=ArbitrationResponse)
async def arbitrate_edges(request: ArbitrationRequest) -> ArbitrationResponse:
    if request.task.image is not None and (
        request.task.image.data_base64 is not None or request.task.image.local_ref is not None
    ):
        raise HTTPException(status_code=422, detail="arbitration requires a byte-free image descriptor")
    trusted_proposals = []
    for proposal in request.proposals:
        status = node_registry.get_node(proposal.node_id)
        trusted_proposals.append(
            proposal.model_copy(
                update={
                    "node_reliability": (
                        status.reliability if status is not None else 0.50
                    )
                }
            )
        )
    trusted_request = request.model_copy(update={"proposals": trusted_proposals})
    try:
        candidate = arbitrate(trusted_request)
        if not request.finalize:
            decision = candidate
            created = True
        else:
            decision, created = await asyncio.to_thread(
                fusion_store().resolve,
                request,
                candidate,
            )
    except (ValueError, FusionConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    event_data = decision.model_dump(mode="json")
    event_type = "arbitration"
    if not request.finalize:
        event_type = "arbitration_preview"
    elif decision.requires_cloud_review:
        event_type = "arbitration_pending"
    elif not created:
        event_type = "arbitration_replay" if decision.idempotent_replay else "late_evidence"
    await record_event(
        task_id=request.task.task_id,
        component="controller",
        event_type=event_type,
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
        attempted_routes=[Route.PEER_EDGE],
        total_latency_ms=round(request.elapsed_ms + (time.perf_counter() - started) * 1000, 3),
    )


@app.post("/v1/escalate", response_model=DecisionResponse)
async def escalate(request: EscalationRequest) -> DecisionResponse:
    if request.task.image is not None:
        raise HTTPException(
            status_code=422,
            detail="visual tasks must use the byte-free /v1/routes/decide control path",
        )
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
                attempted_routes=[Route.PEER_EDGE],
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
            attempted_routes=[Route.PEER_EDGE] if peer_attempted else [],
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
            attempted_routes=(
                [Route.PEER_EDGE, Route.CLOUD]
                if peer_attempted
                else [Route.CLOUD]
            ),
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
                peer_decision = peer_decision.model_copy(
                    update={
                        "attempted_routes": [Route.CLOUD, Route.PEER_EDGE]
                    }
                )
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
            attempted_routes=(
                [Route.PEER_EDGE, Route.CLOUD]
                if peer_attempted
                else [Route.CLOUD]
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
