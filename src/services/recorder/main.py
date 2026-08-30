from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi import FastAPI, Query
from sqlalchemy import DateTime, Integer, String, Text, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from common.schemas import EventCreate

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////tmp/metrics.db")
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
            select(Event).where(Event.event_type == "arbitration")
        ).all()
        arbitration_data = [json.loads(row.data_json) for row in arbitration_rows]
        arbitration_total = len(arbitration_data)
        conflicts = sum(bool(item.get("conflict")) for item in arbitration_data)
        conflict_rows = [item for item in arbitration_data if bool(item.get("conflict"))]
        autonomous_resolutions = sum(
            bool(item.get("resolution_success")) for item in conflict_rows
        )
        labeled_conflicts = [
            item for item in conflict_rows if "resolution_correct" in item
        ]
        correct_resolutions = sum(
            bool(item.get("resolution_correct")) for item in labeled_conflicts
        )
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
        session.commit()
    return {"ok": True}
