from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from common.schemas import EventCreate

_pending_deliveries: set[asyncio.Task[None]] = set()
_MAX_PENDING_DELIVERIES = 256
logger = logging.getLogger(__name__)


async def _deliver_event(event: EventCreate) -> None:
    recorder_url = os.getenv("RECORDER_URL", "http://localhost:8004").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=0.35) as client:
            response = await client.post(
                f"{recorder_url}/v1/events",
                json=event.model_dump(mode="json"),
            )
            response.raise_for_status()
    except (httpx.HTTPError, OSError):
        return


async def record_event(
    *,
    task_id: str,
    component: str,
    event_type: str,
    route: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Queue bounded best-effort telemetry without delaying business responses."""

    event = EventCreate(
        task_id=task_id,
        component=component,
        event_type=event_type,
        route=route,
        data=data or {},
    )
    if len(_pending_deliveries) >= _MAX_PENDING_DELIVERIES:
        logger.warning(
            "dropping recorder event because pending delivery limit was reached: task_id=%s",
            task_id,
        )
        return

    delivery = asyncio.create_task(_deliver_event(event))
    _pending_deliveries.add(delivery)
    delivery.add_done_callback(_pending_deliveries.discard)


async def flush_pending_events(timeout_s: float = 1.0) -> None:
    """Drain queued telemetry during graceful service shutdown."""

    pending = list(_pending_deliveries)
    if not pending:
        return
    try:
        async with asyncio.timeout(timeout_s):
            await asyncio.gather(*pending, return_exceptions=True)
    except TimeoutError:
        for delivery in pending:
            if not delivery.done():
                delivery.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
