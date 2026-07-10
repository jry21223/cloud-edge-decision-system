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
        return {
            "total_events": total,
            "routes": {route: count for route, count in routes},
            "components": {component: count for component, count in components},
        }


@app.post("/v1/reset")
async def reset() -> dict[str, bool]:
    with Session(engine) as session:
        session.execute(delete(Event))
        session.commit()
    return {"ok": True}
