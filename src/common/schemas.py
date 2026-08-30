from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Route(StrEnum):
    EDGE = "EDGE"
    EDGE_SAFETY = "EDGE_SAFETY"
    CLOUD = "CLOUD"
    PEER_EDGE = "PEER_EDGE"
    EDGE_FALLBACK = "EDGE_FALLBACK"


class TaskRequest(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    scene: Literal["industrial", "traffic"] = "industrial"
    payload: dict[str, Any]
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    deadline_ms: int = Field(default=800, ge=50, le=30_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InferenceResult(BaseModel):
    prediction: str
    confidence: float = Field(ge=0.0, le=1.0)
    action: str
    reason: str
    latency_ms: float = Field(ge=0.0)
    model_name: str
    node_id: str


class EscalationRequest(BaseModel):
    task: TaskRequest
    edge_result: InferenceResult
    elapsed_ms: float = Field(default=0.0, ge=0.0)
    origin_node: str = "edge-a"
    hop_count: int = Field(default=0, ge=0, le=3)
    visited_nodes: list[str] = Field(default_factory=list)


class EdgeProposal(BaseModel):
    """One edge node's observation for the same task and policy epoch."""

    node_id: str = Field(min_length=1, max_length=64)
    result: InferenceResult
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    policy_version: str = Field(default="v1", min_length=1, max_length=32)
    node_reliability: float = Field(default=0.85, ge=0.0, le=1.0)
    spatial_consistency: float = Field(default=1.0, ge=0.0, le=1.0)


class ArbitrationRequest(BaseModel):
    task: TaskRequest
    proposals: list[EdgeProposal] = Field(min_length=2, max_length=8)


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


class NodeStatus(NodeHeartbeat):
    last_seen: datetime
    healthy: bool


class DecisionResponse(BaseModel):
    task_id: str
    route: Route
    final_prediction: str
    final_action: str
    final_confidence: float = Field(ge=0.0, le=1.0)
    decision_reason: str
    edge_result: InferenceResult
    cloud_result: InferenceResult | None = None
    peer_result: InferenceResult | None = None
    degraded: bool = False
    total_latency_ms: float = Field(ge=0.0)


class EventCreate(BaseModel):
    task_id: str
    component: str
    event_type: str
    route: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
