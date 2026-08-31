import io
import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, Request, Response
from httpx import AsyncClient as HttpxAsyncClient
from PIL import Image, ImageDraw

from common.schemas import (
    ArbitrationRequest,
    EdgeProposal,
    EscalationRequest,
    ImageRegion,
    InferenceResult,
    Route,
    RoutingCandidate,
    RoutingDecision,
    TaskRequest,
    UploadMode,
    VisionDetection,
)
from common.vision import image_envelope_from_bytes
from services.controller import main as controller_main
from services.edge_node import main as edge_main


def _synthetic_anomaly_png_bytes() -> bytes:
    """Return a generated API fixture, not real industrial data."""

    image = Image.new("RGB", (96, 72), (128, 128, 128))
    drawing = ImageDraw.Draw(image)
    for y in range(0, image.height, 8):
        drawing.rectangle(
            (0, y, image.width - 1, min(y + 3, image.height - 1)),
            fill=(118, 118, 118),
        )
    drawing.rectangle((30, 20, 55, 40), fill=(250, 250, 250))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _synthetic_vision_task(*, task_id: str | None = None) -> TaskRequest:
    resolved_task_id = task_id or f"synthetic-vision-{uuid4()}"
    raw = _synthetic_anomaly_png_bytes()
    return TaskRequest(
        task_id=resolved_task_id,
        trace_id=f"trace-{resolved_task_id}",
        scene="industrial",
        workpiece_id=f"synthetic-workpiece-{resolved_task_id}",
        station_id="synthetic-test-station",
        batch_id="synthetic-test-batch",
        payload={},
        deadline_ms=2_000,
        context={"data_provenance": "synthetic_test_fixture"},
        metadata={
            "cloud_expected_accuracy": 0.99,
            "network": {
                "availability": 1.0,
                "rtt_ms": 5.0,
                "jitter_ms": 0.0,
                "packet_loss": 0.0,
            },
        },
        image=image_envelope_from_bytes(
            raw,
            frame_id=f"synthetic-frame-{resolved_task_id}",
            mime_type="image/png",
            local_ref=f"synthetic://{resolved_task_id}",
        ),
    )


def _synthetic_edge_summary() -> InferenceResult:
    return InferenceResult(
        prediction="unknown_anomaly",
        confidence=0.55,
        action="isolate",
        reason="synthetic contract fixture",
        latency_ms=5.0,
        model_name="synthetic-test-adapter",
        node_id="edge-a",
        model_version="test-v1",
        preprocess_version="synthetic-v1",
        detections=[
            VisionDetection(
                label="unknown_anomaly",
                score=0.80,
                bbox=ImageRegion(x=30, y=20, width=26, height=21),
            )
        ],
    )


async def _ignore_external_recorder_event(**_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_controller_selects_roi_from_a_byte_free_synthetic_summary(monkeypatch):
    monkeypatch.setattr(controller_main, "record_event", _ignore_external_recorder_event)
    task = _synthetic_vision_task(task_id="synthetic-controller-roi")
    control_task = task.control_plane_copy()
    assert control_task.image is not None
    assert control_task.image.data_base64 is None
    assert control_task.image.local_ref is None
    request = EscalationRequest(
        task=control_task,
        edge_result=_synthetic_edge_summary(),
        elapsed_ms=5.0,
        origin_node="edge-a",
        visited_nodes=["edge-a"],
    )

    async with HttpxAsyncClient(
        transport=ASGITransport(app=controller_main.app),
        base_url="http://synthetic-controller",
    ) as client:
        response = await client.post(
            "/v1/routes/decide",
            json=request.model_dump(mode="json"),
        )

    assert response.status_code == 200
    decision = RoutingDecision.model_validate(response.json())
    assert decision.task_id == task.task_id
    assert decision.trace_id == task.trace_id
    assert decision.route == Route.CLOUD
    assert decision.target_node == "cloud"
    assert decision.target_endpoint
    assert decision.upload_mode == UploadMode.ROI
    assert decision.timeout_ms > 0


@pytest.mark.asyncio
async def test_controller_rejects_inline_synthetic_image_bytes():
    task = _synthetic_vision_task(task_id="synthetic-controller-reject-bytes")
    request = EscalationRequest(
        task=task,
        edge_result=_synthetic_edge_summary(),
        elapsed_ms=5.0,
        origin_node="edge-a",
        visited_nodes=["edge-a"],
    )

    async with HttpxAsyncClient(
        transport=ASGITransport(app=controller_main.app),
        base_url="http://synthetic-controller",
    ) as client:
        response = await client.post(
            "/v1/routes/decide",
            json=request.model_dump(mode="json"),
        )

    assert response.status_code == 422
    assert "image bytes" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_legacy_escalation_rejects_visual_payload_instead_of_forwarding_it():
    task = _synthetic_vision_task(task_id="synthetic-legacy-reject")
    request = EscalationRequest(
        task=task,
        edge_result=_synthetic_edge_summary(),
        elapsed_ms=5.0,
        origin_node="edge-a",
        visited_nodes=["edge-a"],
    )

    async with HttpxAsyncClient(
        transport=ASGITransport(app=controller_main.app),
        base_url="http://synthetic-controller",
    ) as client:
        response = await client.post(
            "/v1/escalate",
            json=request.model_dump(mode="json"),
        )

    assert response.status_code == 422
    assert "byte-free" in response.json()["detail"]


@pytest.mark.asyncio
async def test_arbitration_rejects_visual_bytes():
    task = _synthetic_vision_task(task_id="synthetic-arbitration-reject")
    result = _synthetic_edge_summary()
    request = ArbitrationRequest(
        task=task,
        proposals=[
            EdgeProposal(node_id=node, result=result.model_copy(update={"node_id": node}))
            for node in ("edge-a", "edge-b")
        ],
    )
    async with HttpxAsyncClient(
        transport=ASGITransport(app=controller_main.app),
        base_url="http://synthetic-controller",
    ) as client:
        response = await client.post("/v1/arbitrate", json=request.model_dump(mode="json"))
    assert response.status_code == 422
    assert "byte-free" in response.json()["detail"]


@pytest.mark.asyncio
async def test_heartbeat_allowlist_blocks_an_attacker_controlled_upload_endpoint(
    monkeypatch,
):
    monkeypatch.setenv(
        "TRUSTED_NODE_ENDPOINTS",
        "edge-a=http://edge-a:8000,edge-b=http://edge-b:8000,cloud-node=",
    )
    payload = {
        "node_id": "edge-b",
        "endpoint_url": "https://attacker.invalid/collect",
    }

    async with HttpxAsyncClient(
        transport=ASGITransport(app=controller_main.app),
        base_url="http://synthetic-controller",
    ) as client:
        response = await client.post("/v1/nodes/heartbeat", json=payload)

    assert response.status_code == 403
    assert "trusted mapping" in response.json()["detail"]


@pytest.mark.asyncio
async def test_edge_sends_byte_free_control_request_then_direct_roi(monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_CONTROLS", "true")
    monkeypatch.setattr(edge_main, "record_event", _ignore_external_recorder_event)
    task = _synthetic_vision_task()
    task = task.model_copy(
        update={"metadata": {**task.metadata, "force_confidence": 0.55}}
    )
    assert task.image is not None
    original_image_sha256 = task.image.sha256
    observed: dict[str, object] = {}

    cloud_candidate = RoutingCandidate(
        route=Route.CLOUD,
        target_node="synthetic-cloud",
        target_endpoint="http://synthetic-cloud",
        upload_mode=UploadMode.ROI,
        timeout_ms=1_000,
        estimated_finish_ms=100.0,
        score=0.10,
        feasible=True,
        explanation="synthetic control-plane fixture",
    )
    routing_decision = RoutingDecision(
        task_id=task.task_id,
        trace_id=task.trace_id,
        route=Route.CLOUD,
        target_node=cloud_candidate.target_node,
        target_endpoint=cloud_candidate.target_endpoint,
        upload_mode=UploadMode.ROI,
        timeout_ms=cloud_candidate.timeout_ms,
        estimated_finish_ms=cloud_candidate.estimated_finish_ms,
        decision_reason="synthetic control-plane fixture",
        candidate_scores={"CLOUD@synthetic-cloud": 0.10},
        candidates=[cloud_candidate],
    )
    remote_result = InferenceResult(
        prediction="unknown_anomaly",
        confidence=0.91,
        action="isolate",
        reason="synthetic Cloud boundary response",
        latency_ms=12.0,
        model_name="synthetic-cloud-boundary",
        node_id="synthetic-cloud",
        model_version="test-v1",
        preprocess_version="synthetic-v1",
    )

    class ExternalBoundaryClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, **kwargs) -> Response:
            request = Request("POST", url)
            if url.endswith("/v1/routes/decide"):
                observed["controller_request"] = kwargs["json"]
                return Response(
                    200,
                    json=routing_decision.model_dump(mode="json"),
                    request=request,
                )
            if url == "http://synthetic-cloud/v1/infer":
                observed["remote_request"] = json.loads(kwargs["content"])
                observed["remote_headers"] = kwargs["headers"]
                return Response(
                    200,
                    content=remote_result.model_dump_json().encode("utf-8"),
                    headers={"content-type": "application/json"},
                    request=request,
                )
            raise AssertionError(f"unexpected external URL: {url}")

    monkeypatch.setattr(edge_main.httpx, "AsyncClient", ExternalBoundaryClient)

    async with HttpxAsyncClient(
        transport=ASGITransport(app=edge_main.app),
        base_url="http://synthetic-edge",
    ) as client:
        response = await client.post("/v1/tasks", json=task.model_dump(mode="json"))

    assert response.status_code == 200
    decision = response.json()
    assert decision["route"] == "CLOUD"
    assert decision["upload_mode"] == "ROI"
    assert decision["uploaded_bytes"] > 0

    controller_request = observed["controller_request"]
    assert isinstance(controller_request, dict)
    controller_image = controller_request["task"]["image"]
    assert controller_image["data_base64"] is None
    assert controller_image["local_ref"] is None
    assert controller_image["sha256"] == original_image_sha256

    remote_request = observed["remote_request"]
    assert isinstance(remote_request, dict)
    remote_image = remote_request["image"]
    assert remote_image["data_base64"]
    assert remote_image["local_ref"] is None
    assert remote_image["sha256"] != original_image_sha256
    assert remote_image["width"] < task.image.width
    assert remote_image["height"] < task.image.height
    assert remote_request["metadata"]["upload_mode"] == "ROI"
    assert remote_request["metadata"]["source_image_sha256"] == original_image_sha256
    remote_headers = observed["remote_headers"]
    assert isinstance(remote_headers, dict)
    assert remote_headers["Idempotency-Key"]


def test_raw_upload_strips_edge_local_reference():
    task = _synthetic_vision_task(task_id="synthetic-raw-no-local-ref")
    assert task.image is not None and task.image.local_ref is not None

    uploaded = edge_main._prepare_upload_task(
        task,
        _synthetic_edge_summary().model_copy(update={"detections": []}),
        UploadMode.RAW,
    )

    assert uploaded.image is not None
    assert uploaded.image.data_base64 is not None
    assert uploaded.image.local_ref is None
