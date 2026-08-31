import asyncio

import pytest

from common import recorder_client


@pytest.mark.asyncio
async def test_record_event_does_not_wait_for_delivery(monkeypatch):
    delivery_started = asyncio.Event()
    allow_delivery = asyncio.Event()

    async def slow_delivery(_event):
        delivery_started.set()
        await allow_delivery.wait()

    monkeypatch.setattr(recorder_client, "_deliver_event", slow_delivery)

    await recorder_client.record_event(
        task_id="telemetry-1",
        component="edge-a",
        event_type="decision",
        route="EDGE",
    )

    await asyncio.wait_for(delivery_started.wait(), timeout=0.1)
    assert recorder_client._pending_deliveries

    allow_delivery.set()
    await asyncio.gather(*list(recorder_client._pending_deliveries))
    assert not recorder_client._pending_deliveries


@pytest.mark.asyncio
async def test_record_event_caps_pending_deliveries(monkeypatch, caplog):
    allow_delivery = asyncio.Event()

    async def slow_delivery(_event):
        await allow_delivery.wait()

    monkeypatch.setattr(recorder_client, "_deliver_event", slow_delivery)
    monkeypatch.setattr(recorder_client, "_MAX_PENDING_DELIVERIES", 2)

    for index in range(3):
        await recorder_client.record_event(
            task_id=f"telemetry-{index}",
            component="edge-a",
            event_type="decision",
        )

    assert len(recorder_client._pending_deliveries) == 2
    assert "pending delivery limit was reached" in caplog.text

    allow_delivery.set()
    await recorder_client.flush_pending_events()
    assert not recorder_client._pending_deliveries


@pytest.mark.asyncio
async def test_flush_pending_events_cancels_slow_deliveries(monkeypatch):
    async def never_finishes(_event):
        await asyncio.Event().wait()

    monkeypatch.setattr(recorder_client, "_deliver_event", never_finishes)
    await recorder_client.record_event(
        task_id="telemetry-timeout",
        component="controller",
        event_type="decision",
    )

    await recorder_client.flush_pending_events(timeout_s=0.01)

    assert not recorder_client._pending_deliveries
