from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from common.schemas import ArbitrationRequest, ArbitrationResponse


class FusionConflictError(ValueError):
    pass


class FusionStore:
    """Persist one immutable autonomous fusion decision per task association."""

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
                CREATE TABLE IF NOT EXISTS fusion_groups (
                    association_id TEXT PRIMARY KEY,
                    task_sha256 TEXT NOT NULL,
                    proposal_sha256 TEXT,
                    decision_json TEXT NOT NULL,
                    finalized_at REAL NOT NULL,
                    late_proposals_ignored INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(fusion_groups)"
                ).fetchall()
            }
            if "proposal_sha256" not in columns:
                self._connection.execute(
                    "ALTER TABLE fusion_groups ADD COLUMN proposal_sha256 TEXT"
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def task_hash(request: ArbitrationRequest) -> str:
        task = request.task.model_dump(mode="json")
        metadata = dict(task.get("metadata") or {})
        for key in tuple(metadata):
            if key.startswith("ground_truth"):
                metadata.pop(key, None)
        task["metadata"] = metadata
        canonical = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def proposal_hash(request: ArbitrationRequest) -> str:
        proposals = [
            proposal.model_dump(mode="json")
            for proposal in sorted(
                request.proposals,
                key=lambda item: (item.node_id, item.proposal_id),
            )
        ]
        canonical = json.dumps(
            proposals,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def resolve(
        self,
        request: ArbitrationRequest,
        decision: ArbitrationResponse,
    ) -> tuple[ArbitrationResponse, bool]:
        association_id = request.association_id or request.task.task_id
        task_sha256 = self.task_hash(request)
        proposal_sha256 = self.proposal_hash(request)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM fusion_groups WHERE association_id = ?",
                (association_id,),
            ).fetchone()
            if row is not None:
                if row["task_sha256"] != task_sha256:
                    raise FusionConflictError(
                        "association_id already finalized for a different task payload"
                    )
                cached = ArbitrationResponse.model_validate_json(row["decision_json"]).model_copy(
                    update={"late_proposals_ignored": int(row["late_proposals_ignored"])}
                )
                if row["proposal_sha256"] == proposal_sha256:
                    return cached.model_copy(update={"idempotent_replay": True}), False
                late_count = int(row["late_proposals_ignored"]) + len(request.proposals)
                self._connection.execute(
                    """
                    UPDATE fusion_groups
                    SET late_proposals_ignored = ?
                    WHERE association_id = ?
                    """,
                    (late_count, association_id),
                )
                return cached.model_copy(update={"late_proposals_ignored": late_count}), False

            terminal = decision.model_copy(update={"association_id": association_id})
            if not request.finalize or decision.requires_cloud_review:
                return terminal, False
            self._connection.execute(
                """
                INSERT INTO fusion_groups
                    (association_id, task_sha256, proposal_sha256, decision_json, finalized_at,
                     late_proposals_ignored)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    association_id,
                    task_sha256,
                    proposal_sha256,
                    terminal.model_dump_json(),
                    time.time(),
                ),
            )
        return terminal, True

    def get(self, association_id: str) -> ArbitrationResponse | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT decision_json, late_proposals_ignored FROM fusion_groups "
                "WHERE association_id = ?",
                (association_id,),
            ).fetchone()
        if row is None:
            return None
        decision = ArbitrationResponse.model_validate_json(row["decision_json"])
        return decision.model_copy(
            update={"late_proposals_ignored": int(row["late_proposals_ignored"])}
        )
