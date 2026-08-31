from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class Route(StrEnum):
    EDGE = "EDGE"
    EDGE_SAFETY = "EDGE_SAFETY"
    CLOUD = "CLOUD"
    PEER_EDGE = "PEER_EDGE"
    EDGE_FALLBACK = "EDGE_FALLBACK"


class UploadMode(StrEnum):
    METADATA = "METADATA"
    ROI = "ROI"
    RAW = "RAW"


class ImageRegion(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class ImageEnvelope(BaseModel):
    """Image descriptor shared by the control and data planes.

    ``data_base64`` is present only on Edge/Peer/Cloud data-plane requests. The
    Controller accepts the same descriptor after Edge removes that field.
    """

    frame_id: str = Field(min_length=1, max_length=128)
    width: int = Field(ge=1, le=32_768)
    height: int = Field(ge=1, le=32_768)
    mime_type: Literal["image/jpeg", "image/png", "image/bmp"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1, le=25_000_000)
    local_ref: str | None = Field(default=None, max_length=512)
    roi: ImageRegion | None = None
    data_base64: str | None = Field(default=None, max_length=34_000_000, repr=False)

    @model_validator(mode="after")
    def validate_image(self) -> ImageEnvelope:
        if self.roi is not None and (
            self.roi.x + self.roi.width > self.width
            or self.roi.y + self.roi.height > self.height
        ):
            raise ValueError("roi must stay inside the declared image dimensions")
        if self.data_base64 is None:
            return self
        try:
            raw = base64.b64decode(self.data_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("data_base64 must contain valid base64") from error
        if len(raw) != self.byte_size:
            raise ValueError("byte_size does not match decoded image bytes")
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("sha256 does not match decoded image bytes")
        return self

    def control_descriptor(self) -> ImageEnvelope:
        """Return the byte-free descriptor that may enter the control plane."""

        return self.model_copy(update={"data_base64": None, "local_ref": None})


class ImageQuality(BaseModel):
    brightness: float = Field(ge=0.0, le=1.0)
    contrast: float = Field(ge=0.0, le=1.0)
    sharpness: float = Field(ge=0.0, le=1.0)
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class VisionDetection(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    score: float = Field(ge=0.0, le=1.0)
    bbox: ImageRegion
    severity: Literal["low", "medium", "high", "critical"] = "medium"


class TaskRequest(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    scene: Literal["industrial", "traffic"] = "industrial"
    workpiece_id: str | None = Field(default=None, max_length=128)
    station_id: str | None = Field(default=None, max_length=128)
    batch_id: str | None = Field(default=None, max_length=128)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    image: ImageEnvelope | None = None
    payload: dict[str, Any]
    context: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    deadline_ms: int = Field(default=800, ge=50, le=30_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def control_plane_copy(self) -> TaskRequest:
        """Strip inline image bytes before the task reaches Controller."""

        if self.image is None:
            return self
        return self.model_copy(update={"image": self.image.control_descriptor()})


class InferenceResult(BaseModel):
    prediction: str
    confidence: float = Field(ge=0.0, le=1.0)
    action: str
    reason: str
    latency_ms: float = Field(ge=0.0)
    model_name: str
    node_id: str
    model_version: str = "v1"
    preprocess_version: str = "none"
    detections: list[VisionDetection] = Field(default_factory=list)
    image_quality: ImageQuality | None = None
    anomaly_score: float | None = Field(default=None, ge=0.0, le=1.0)
    scene_complexity: float | None = Field(default=None, ge=0.0, le=1.0)


class EscalationRequest(BaseModel):
    task: TaskRequest
    edge_result: InferenceResult
    elapsed_ms: float = Field(default=0.0, ge=0.0)
    origin_node: str = "edge-a"
    hop_count: int = Field(default=0, ge=0, le=3)
    visited_nodes: list[str] = Field(default_factory=list)


class EdgeProposal(BaseModel):
    """One edge node's observation for the same task and policy epoch."""

    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    node_id: str = Field(min_length=1, max_length=64)
    result: InferenceResult
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    policy_version: str = Field(default="v1", min_length=1, max_length=32)
    node_reliability: float = Field(default=0.85, ge=0.0, le=1.0)
    spatial_consistency: float = Field(default=1.0, ge=0.0, le=1.0)


class ArbitrationRequest(BaseModel):
    task: TaskRequest
    proposals: list[EdgeProposal] = Field(min_length=2, max_length=8)
    association_id: str | None = Field(default=None, max_length=256)
    finalize: bool = True


class ArbitrationResponse(BaseModel):
    task_id: str
    route: Route
    chosen_node: str
    final_prediction: str
    final_action: str
    final_confidence: float = Field(ge=0.0, le=1.0)
    conflict: bool
    resolution_success: bool
    resolution_reason: str
    consensus_score: float = Field(default=1.0, ge=0.0, le=1.0)
    requires_cloud_review: bool = False
    evidence_scores: dict[str, float] = Field(default_factory=dict)
    association_id: str | None = None
    duplicate_proposals: int = Field(default=0, ge=0)
    late_proposals_ignored: int = Field(default=0, ge=0)
    idempotent_replay: bool = False


class RoutingCandidate(BaseModel):
    route: Route
    target_node: str | None = None
    target_endpoint: str | None = None
    upload_mode: UploadMode = UploadMode.METADATA
    timeout_ms: int = Field(ge=0)
    estimated_finish_ms: float = Field(ge=0.0)
    score: float
    feasible: bool
    explanation: str


class RoutingDecision(BaseModel):
    task_id: str
    trace_id: str
    route: Route
    target_node: str | None = None
    target_endpoint: str | None = None
    upload_mode: UploadMode = UploadMode.METADATA
    timeout_ms: int = Field(ge=0)
    estimated_finish_ms: float = Field(ge=0.0)
    decision_reason: str
    policy_version: str = "dream-route-v1"
    candidate_scores: dict[str, float] = Field(default_factory=dict)
    rejected_reasons: list[str] = Field(default_factory=list)
    candidates: list[RoutingCandidate] = Field(default_factory=list)
    network_snapshot: dict[str, float] = Field(default_factory=dict)


class NodeHeartbeat(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    endpoint_url: str | None = Field(default=None, max_length=256)
    supported_scenes: list[Literal["industrial", "traffic"]] = Field(
        default_factory=lambda: ["industrial", "traffic"]
    )
    load: float = Field(default=0.0, ge=0.0, le=1.0)
    queue_depth: int = Field(default=0, ge=0)
    estimated_latency_ms: float = Field(default=20.0, ge=0.0)
    model_version: str = Field(default="rule-v0.1", min_length=1, max_length=64)
    reliability: float = Field(default=0.85, ge=0.0, le=1.0)
    cpu_utilization: float | None = Field(default=None, ge=0.0, le=1.0)
    memory_utilization: float | None = Field(default=None, ge=0.0, le=1.0)
    process_rss_mb: float | None = Field(default=None, ge=0.0)
    gpu_utilization: float | None = Field(default=None, ge=0.0, le=1.0)
    gpu_memory_used_mb: float | None = Field(default=None, ge=0.0)
    rtt_ms: float | None = Field(default=None, ge=0.0)
    bandwidth_mbps: float | None = Field(default=None, ge=0.0)
    telemetry_source: str = Field(default="runtime", max_length=64)


class NodeStatus(NodeHeartbeat):
    last_seen: datetime
    healthy: bool


class DecisionResponse(BaseModel):
    task_id: str
    trace_id: str | None = None
    association_id: str | None = None
    route: Route
    final_prediction: str
    final_action: str
    final_confidence: float = Field(ge=0.0, le=1.0)
    decision_reason: str
    edge_result: InferenceResult
    cloud_result: InferenceResult | None = None
    peer_result: InferenceResult | None = None
    arbitration: ArbitrationResponse | None = None
    upload_mode: UploadMode | None = None
    uploaded_bytes: int = Field(default=0, ge=0)
    outbox_id: str | None = None
    degraded: bool = False
    total_latency_ms: float = Field(ge=0.0)
    deadline_met: bool | None = None
    attempted_routes: list[Route] = Field(default_factory=list)


class GroundTruthCreate(BaseModel):
    prediction: str = Field(min_length=1, max_length=128)
    action: str | None = Field(default=None, max_length=128)
    source: str = Field(default="manual", min_length=1, max_length=128)


class EventCreate(BaseModel):
    task_id: str
    component: str
    event_type: str
    route: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
