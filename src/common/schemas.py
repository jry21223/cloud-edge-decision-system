from __future__ import annotations

from datetime import datetime, timezone
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


class DecisionResponse(BaseModel):
    task_id: str
    route: Route
    final_prediction: str
    final_action: str
    final_confidence: float = Field(ge=0.0, le=1.0)
    decision_reason: str
    edge_result: InferenceResult
    cloud_result: InferenceResult | None = None
    degraded: bool = False
    total_latency_ms: float = Field(ge=0.0)


class EventCreate(BaseModel):
    task_id: str
    component: str
    event_type: str
    route: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
