from datetime import UTC, datetime, timedelta

from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from fastapi import APIRouter
from sqlalchemy import select

router = APIRouter(prefix="/audit", tags=["audit"])

READ_ACTIONS = {"runtime_context", "runtime_ask", "read"}
WRITE_ACTIONS = {"write", "upgrade", "edit", "delete", "transcript_write"}

@router.get("")
def list_audit():
    db = SessionLocal()
    try:
        rows = db.execute(
            select(MemoryAudit).order_by(MemoryAudit.created_at.desc())
        ).scalars().all()

        return [
            {
                "id": row.id,
                "action": row.action,
                "memory_id": row.memory_id,
                "payload_json": row.payload_json,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    finally:
        db.close()


@router.get("/metrics")
def audit_metrics(hours: int = 24):
    bounded_hours = max(1, min(int(hours or 24), 168))
    cutoff = datetime.now(UTC) - timedelta(hours=bounded_hours)
    db = SessionLocal()
    try:
        rows = db.execute(
            select(MemoryAudit.action, MemoryAudit.created_at).where(MemoryAudit.created_at >= cutoff)
        ).all()
        by_action: dict[str, int] = {}
        reads = 0
        writes = 0
        for action, _created_at in rows:
            key = str(action or "unknown")
            by_action[key] = by_action.get(key, 0) + 1
            if key in READ_ACTIONS:
                reads += 1
            if key in WRITE_ACTIONS:
                writes += 1
        return {
            "window_hours": bounded_hours,
            "reads": reads,
            "writes": writes,
            "by_action": by_action,
        }
    finally:
        db.close()
