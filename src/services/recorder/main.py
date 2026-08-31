from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import DateTime, Integer, String, Text, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from common.schemas import EventCreate, GroundTruthCreate

_DEFAULT_DATABASE = Path(tempfile.gettempdir()) / "cloud-edge-metrics.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DEFAULT_DATABASE.as_posix()}")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    task_id: Mapped[str] = mapped_column(String(128), index=True)
    component: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    route: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    data_json: Mapped[str] = mapped_column(Text)


class GroundTruth(Base):
    __tablename__ = "ground_truth"

    association_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    prediction: Mapped[str] = mapped_column(String(128))
    action: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(128))
    attached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


Base.metadata.create_all(engine)
app = FastAPI(title="Cloud-Edge MVP - Recorder", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/events")
async def create_event(event: EventCreate) -> dict[str, int]:
    row = Event(
        created_at=event.created_at,
        task_id=event.task_id,
        component=event.component,
        event_type=event.event_type,
        route=event.route,
        data_json=json.dumps(event.data, ensure_ascii=False),
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"id": row.id}


@app.put("/v1/ground-truth/{association_id}")
async def put_ground_truth(
    association_id: str,
    truth: GroundTruthCreate,
) -> dict[str, object]:
    """Attach truth after inference; labels never enter Edge/Cloud requests."""

    with Session(engine) as session:
        existing = session.get(GroundTruth, association_id)
        if existing is not None:
            if (
                existing.prediction != truth.prediction
                or existing.action != truth.action
                or existing.source != truth.source
            ):
                raise HTTPException(
                    status_code=409,
                    detail="ground truth already exists with different content",
                )
            return {"association_id": association_id, "created": False}
        session.add(
            GroundTruth(
                association_id=association_id,
                prediction=truth.prediction,
                action=truth.action,
                source=truth.source,
                attached_at=datetime.now(UTC),
            )
        )
        session.commit()
    return {"association_id": association_id, "created": True}


@app.get("/v1/events")
async def list_events(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, object]]:
    with Session(engine) as session:
        rows = session.scalars(select(Event).order_by(Event.id.desc()).limit(limit)).all()
        return [
            {
                "id": row.id,
                "created_at": row.created_at.isoformat(),
                "task_id": row.task_id,
                "component": row.component,
                "event_type": row.event_type,
                "route": row.route,
                "data": json.loads(row.data_json),
            }
            for row in rows
        ]


@app.get("/v1/summary")
async def summary() -> dict[str, object]:
    with Session(engine) as session:
        total = session.scalar(select(func.count(Event.id))) or 0
        routes = session.execute(
            select(Event.route, func.count(Event.id)).where(Event.route.is_not(None), Event.event_type == "decision").group_by(Event.route)
        ).all()
        components = session.execute(select(Event.component, func.count(Event.id)).group_by(Event.component)).all()
        arbitration_rows = session.scalars(
            select(Event)
            .where(Event.event_type.in_(("arbitration", "arbitration_pending")))
            .order_by(Event.id)
        ).all()
        arbitration_by_association: dict[str, dict[str, object]] = {}
        for row in arbitration_rows:
            item = json.loads(row.data_json)
            association_id = str(item.get("association_id") or row.task_id)
            arbitration_by_association.setdefault(association_id, item)
        arbitration_data = list(arbitration_by_association.values())
        arbitration_total = len(arbitration_by_association)
        conflicts = sum(bool(item.get("conflict")) for item in arbitration_data)
        conflict_rows = {
            association_id: item
            for association_id, item in arbitration_by_association.items()
            if bool(item.get("conflict"))
        }
        autonomous_resolutions = sum(
            bool(item.get("resolution_success")) for item in conflict_rows.values()
        )
        truth_by_association = {
            row.association_id: row
            for row in session.scalars(select(GroundTruth)).all()
        }
        final_by_association: dict[str, dict[str, object]] = {}
        decision_rows = session.scalars(
            select(Event).where(Event.event_type == "decision").order_by(Event.id)
        ).all()
        for row in decision_rows:
            item = json.loads(row.data_json)
            association_id = item.get("association_id")
            if association_id:
                final_by_association[str(association_id)] = item
        labeled_conflicts = {
            association_id: item
            for association_id, item in conflict_rows.items()
            if association_id in truth_by_association
        }
        correct_resolutions = 0
        for association_id, arbitration_item in labeled_conflicts.items():
            truth = truth_by_association[association_id]
            final = final_by_association.get(association_id)
            if final is None:
                final = arbitration_item if arbitration_item.get("resolution_success") else {}
            prediction_matches = final.get("final_prediction") == truth.prediction
            action_matches = truth.action is None or final.get("final_action") == truth.action
            correct_resolutions += bool(prediction_matches and action_matches)
        return {
            "total_events": total,
            "routes": {route: count for route, count in routes},
            "components": {component: count for component, count in components},
            "arbitration": {
                "total": arbitration_total,
                "conflict_count": conflicts,
                "conflict_rate": 0 if arbitration_total == 0 else round(conflicts / arbitration_total, 4),
                "autonomous_resolution_rate": (
                    0 if conflicts == 0 else round(autonomous_resolutions / conflicts, 4)
                ),
                "resolution_success_rate": (
                    None
                    if not labeled_conflicts
                    else round(correct_resolutions / len(labeled_conflicts), 4)
                ),
                "labeled_conflicts": len(labeled_conflicts),
            },
        }


@app.post("/v1/reset")
async def reset() -> dict[str, bool]:
    with Session(engine) as session:
        session.execute(delete(Event))
        session.execute(delete(GroundTruth))
        session.commit()
    return {"ok": True}
