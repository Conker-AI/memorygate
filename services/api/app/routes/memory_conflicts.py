"""Memory routes for conflicts and revision history."""

import json
from datetime import UTC, datetime

from app.core.agent import get_agent_id
from app.core.db import SessionLocal
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from app.schemas.memory import ConflictResolveRequest
from app.services.memory_truth import add_revision
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

router = APIRouter()


@router.get("/conflicts")
def list_conflicts(agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        rows = db.execute(select(MemoryConflict).where(MemoryConflict.agent_id == agent_id).order_by(MemoryConflict.created_at.desc())).scalars().all()
        return {"results": [{"id": row.id, "memory_id": row.memory_id, "conflicting_memory_id": row.conflicting_memory_id,
                              "reason": row.reason, "confidence": row.confidence, "status": row.status,
                              "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}
    finally:
        db.close()


@router.get("/{memory_id}/revisions")
def memory_revisions(memory_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        memory = db.get(Memory, memory_id)
        if not memory or memory.agent_id != agent_id:
            raise HTTPException(404, "Memory not found")
        rows = db.execute(select(MemoryRevision).where(MemoryRevision.memory_id == memory_id).order_by(MemoryRevision.created_at.desc())).scalars().all()
        return {"results": [{"id": row.id, "changed_by": row.changed_by, "reason": row.reason,
                              "snapshot": json.loads(row.snapshot_json or "{}"), "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}
    finally:
        db.close()


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, payload: ConflictResolveRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        conflict = db.get(MemoryConflict, conflict_id)
        if not conflict or conflict.agent_id != agent_id:
            raise HTTPException(404, "Conflict not found")
        if payload.winner_memory_id not in (conflict.memory_id, conflict.conflicting_memory_id):
            raise HTTPException(422, "Winner must be one of the conflicted memories")
        winner = db.get(Memory, payload.winner_memory_id)
        loser = db.get(Memory, conflict.conflicting_memory_id if payload.winner_memory_id == conflict.memory_id else conflict.memory_id)
        if winner:
            winner.status = "active"
            winner.valid_until = None
            add_revision(db, winner, "selected during conflict resolution", "user")
        if loser:
            loser.status = "needs_review"
            loser.valid_until = datetime.now(UTC)
            add_revision(db, loser, "not selected during conflict resolution", "user")
        conflict.status = "resolved"
        conflict.resolved_by = "user"
        conflict.resolved_at = datetime.now(UTC)
        db.commit()
        return {"status": "ok", "winner_memory_id": payload.winner_memory_id}
    except StaleDataError:
        # Another resolution or edit changed one of these memories first; that one stands.
        db.rollback()
        raise HTTPException(409, {"code": "revision_conflict", "message": "Memory changed; reload before resolving."}) from None
    finally:
        db.close()
