from datetime import UTC, datetime, timedelta

from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from fastapi import APIRouter
from sqlalchemy import func, select

router = APIRouter(prefix="/audit", tags=["audit"])

READ_ACTIONS = {"runtime_context", "runtime_ask", "read"}
WRITE_ACTIONS = {"write", "upgrade", "edit", "delete", "transcript_write", "transcript_delete"}

DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


@router.get("")
def list_audit(limit: int = DEFAULT_LIMIT):
    """The newest audit rows, at most `limit` (1..2000): the table grows with every read."""
    db = SessionLocal()
    try:
        rows = db.execute(
            select(MemoryAudit).order_by(MemoryAudit.created_at.desc(), MemoryAudit.id.desc())
            .limit(max(1, min(limit, MAX_LIMIT)))
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
        counts = db.execute(
            select(MemoryAudit.action, func.count()).where(MemoryAudit.created_at >= cutoff)
            .group_by(MemoryAudit.action)
        ).all()
        by_action: dict[str, int] = {}
        for action, count in counts:
            key = str(action or "unknown")
            by_action[key] = by_action.get(key, 0) + count
        reads = sum(count for key, count in by_action.items() if key in READ_ACTIONS)
        writes = sum(count for key, count in by_action.items() if key in WRITE_ACTIONS)
        return {
            "window_hours": bounded_hours,
            "reads": reads,
            "writes": writes,
            "by_action": by_action,
        }
    finally:
        db.close()
