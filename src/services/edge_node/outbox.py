from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


@dataclass(frozen=True)
class OutboxItem:
    item_id: str
    task_id: str
    idempotency_key: str
    target_url: str
    payload: dict[str, Any]
    attempts: int


class RequestConflictError(ValueError):
    pass


class EdgeStateStore:
    """Small durable Edge state store for action idempotency and late review."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    task_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    action TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    committed_at REAL NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    item_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    target_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    last_error TEXT
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def request_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def cached_response(
        self,
        *,
        task_id: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        request_sha256 = self.request_hash(request_payload)
        with self._lock:
            row = self._connection.execute(
                "SELECT request_sha256, response_json FROM actions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise RequestConflictError("task_id already exists with different request content")
        return json.loads(row["response_json"])

    def commit_action(
        self,
        *,
        task_id: str,
        request_payload: dict[str, Any],
        action: str,
        response_payload: dict[str, Any],
    ) -> bool:
        request_sha256 = self.request_hash(request_payload)
        response_json = json.dumps(response_payload, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO actions
                    (task_id, request_sha256, action, response_json, committed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, request_sha256, action, response_json, time.time()),
            )
            if cursor.rowcount:
                return True
            row = self._connection.execute(
                "SELECT request_sha256 FROM actions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is not None and row["request_sha256"] != request_sha256:
            raise RequestConflictError("task_id already exists with different request content")
        return False

    def enqueue_review(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        target_url: str,
        payload: dict[str, Any],
    ) -> str:
        item_id = str(uuid4())
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO outbox
                    (item_id, task_id, idempotency_key, target_url, payload_json,
                     state, attempts, next_attempt_at, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (item_id, task_id, idempotency_key, target_url, payload_json, now, now),
            )
            row = self._connection.execute(
                """
                SELECT item_id, task_id, target_url, payload_json
                FROM outbox
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to persist outbox item")
        if (
            row["task_id"] != task_id
            or row["target_url"] != target_url
            or row["payload_json"] != payload_json
        ):
            raise RequestConflictError(
                "idempotency_key already exists with different outbox content"
            )
        return str(row["item_id"])

    def pending(self, *, now: float | None = None, limit: int = 16) -> list[OutboxItem]:
        current = time.time() if now is None else now
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT item_id, task_id, idempotency_key, target_url, payload_json, attempts
                FROM outbox
                WHERE state = 'pending' AND next_attempt_at <= ?
                ORDER BY created_at, item_id
                LIMIT ?
                """,
                (current, limit),
            ).fetchall()
        return [
            OutboxItem(
                item_id=str(row["item_id"]),
                task_id=str(row["task_id"]),
                idempotency_key=str(row["idempotency_key"]),
                target_url=str(row["target_url"]),
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def mark_delivered(self, item_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE outbox
                SET state = 'delivered', delivered_at = ?, last_error = NULL
                WHERE item_id = ?
                """,
                (time.time(), item_id),
            )

    def mark_failed(self, item_id: str, *, attempts: int, error: str) -> None:
        schedule = (1, 2, 5, 10, 30, 60)
        delay = schedule[min(max(attempts, 1) - 1, len(schedule) - 1)]
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE outbox
                SET attempts = ?, next_attempt_at = ?, last_error = ?
                WHERE item_id = ?
                """,
                (attempts, time.time() + delay, error[:500], item_id),
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT state, COUNT(*) AS count FROM outbox GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}


async def flush_outbox_once(
    store: EdgeStateStore,
    *,
    timeout_s: float = 2.0,
) -> dict[str, int]:
    delivered = 0
    failed = 0
    for item in await _to_thread(store.pending):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                response = await client.post(
                    f"{item.target_url.rstrip('/')}/v1/infer",
                    json=item.payload,
                    headers={
                        "Idempotency-Key": item.idempotency_key,
                        "X-Review-Only": "true",
                    },
                )
                response.raise_for_status()
            await _to_thread(store.mark_delivered, item.item_id)
            delivered += 1
        except (httpx.HTTPError, OSError, ValueError) as error:
            await _to_thread(
                store.mark_failed,
                item.item_id,
                attempts=item.attempts + 1,
                error=f"{type(error).__name__}: {error}",
            )
            failed += 1
    return {"delivered": delivered, "failed": failed}


async def _to_thread(function, /, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(function, *args, **kwargs)
