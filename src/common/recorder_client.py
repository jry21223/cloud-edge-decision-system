from __future__ import annotations

import os
from typing import Any

import httpx

from common.schemas import EventCreate


async def record_event(
    *,
    task_id: str,
    component: str,
    event_type: str,
    route: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Best-effort telemetry. Business paths must not fail because metrics are down."""
    recorder_url = os.getenv("RECORDER_URL", "http://localhost:8004").rstrip("/")
    event = EventCreate(
        task_id=task_id,
        component=component,
        event_type=event_type,
        route=route,
        data=data or {},
    )
    try:
        async with httpx.AsyncClient(timeout=0.35) as client:
            await client.post(f"{recorder_url}/v1/events", json=event.model_dump(mode="json"))
    except (httpx.HTTPError, OSError):
        return
