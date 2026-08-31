import httpx
import pytest

from services.edge_node import outbox
from services.edge_node.outbox import EdgeStateStore, RequestConflictError


class _OkResponse:
    def raise_for_status(self) -> None:
        return None


def test_action_commit_is_idempotent_and_rejects_same_task_with_new_content(tmp_path):
    store = EdgeStateStore(tmp_path / "edge.db")
    request = {"task_id": "task-1", "payload": {"temperature": 80}}
    response = {"route": "EDGE_SAFETY", "final_action": "shutdown"}

    try:
        assert store.commit_action(
            task_id="task-1",
            request_payload=request,
            action="shutdown",
            response_payload=response,
        )
        assert not store.commit_action(
            task_id="task-1",
            request_payload=request,
            action="shutdown",
            response_payload=response,
        )
        assert store.cached_response(task_id="task-1", request_payload=request) == response

        with pytest.raises(RequestConflictError, match="different request content"):
            store.cached_response(
                task_id="task-1",
                request_payload={"task_id": "task-1", "payload": {"temperature": 20}},
            )
    finally:
        store.close()


def test_outbox_rejects_reusing_key_for_different_content(tmp_path):
    store = EdgeStateStore(tmp_path / "edge.db")
    try:
        store.enqueue_review(
            task_id="task-late",
            idempotency_key="stable-key",
            target_url="http://cloud-node:8000",
            payload={"task_id": "task-late", "payload": {"sample": 1}},
        )
        with pytest.raises(RequestConflictError, match="different outbox content"):
            store.enqueue_review(
                task_id="task-late",
                idempotency_key="stable-key",
                target_url="http://cloud-node:8000",
                payload={"task_id": "task-late", "payload": {"sample": 2}},
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_outbox_retries_after_disconnect_and_delivers_only_once(
    tmp_path, monkeypatch
):
    clock = {"now": 1_000.0}
    post_calls: list[dict[str, object]] = []
    successful_keys: list[str] = []

    class RetryClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, json, headers):
            post_calls.append({"url": url, "json": json, "headers": headers})
            if len(post_calls) == 1:
                raise httpx.ConnectError("network disconnected")
            successful_keys.append(headers["Idempotency-Key"])
            return _OkResponse()

    monkeypatch.setattr(outbox.time, "time", lambda: clock["now"])
    monkeypatch.setattr(outbox.httpx, "AsyncClient", RetryClient)

    database = tmp_path / "edge.db"
    store = EdgeStateStore(database)
    payload = {"task_id": "task-late", "payload": {"sample": 1}}
    key = "late-review:task-late:image-sha"
    first_id = store.enqueue_review(
        task_id="task-late",
        idempotency_key=key,
        target_url="http://cloud-node:8000",
        payload=payload,
    )
    duplicate_id = store.enqueue_review(
        task_id="task-late",
        idempotency_key=key,
        target_url="http://cloud-node:8000",
        payload=payload,
    )
    assert duplicate_id == first_id

    first_flush = await outbox.flush_outbox_once(store)
    assert first_flush == {"delivered": 0, "failed": 1}
    assert store.counts() == {"pending": 1}
    store.close()

    # The failed item and its retry schedule survive reopening the SQLite file.
    reopened = EdgeStateStore(database)
    try:
        assert await outbox.flush_outbox_once(reopened) == {"delivered": 0, "failed": 0}
        clock["now"] += 1.0
        assert await outbox.flush_outbox_once(reopened) == {"delivered": 1, "failed": 0}
        assert await outbox.flush_outbox_once(reopened) == {"delivered": 0, "failed": 0}

        assert len(post_calls) == 2
        assert successful_keys == [key]
        assert post_calls[0]["headers"] == post_calls[1]["headers"]
        assert reopened.counts() == {"delivered": 1}
    finally:
        reopened.close()
