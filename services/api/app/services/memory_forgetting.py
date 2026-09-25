"""Forgetting a memory removes every stored copy of its text.

The memory row, its revision snapshots, conflict records naming it, and the text in
its audit entries are removed or redacted in one transaction. A content-free audit
entry and a deletion receipt (for recovery replay) record that it happened. The
vector point is removed after commit; a failed removal leaves a visible receipt.

The source conversation is not touched: forgetting a memory is not forgetting the
chat it came from.
"""

import hashlib
import json

from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from app.models import deletion_receipt
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_forget import MemoryForget
from app.models.memory_revision import MemoryRevision
from app.services.qdrant_store import delete_memory_embedding, index_after_commit
from fastapi import HTTPException
from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


def erase(db, agent_id: str, memory: Memory, action: str, detail: dict) -> None:
    """Remove the memory and every text copy inside the caller's transaction."""
    db.execute(delete(MemoryRevision).where(MemoryRevision.memory_id == memory.id))
    db.execute(
        delete(MemoryConflict).where(
            or_(
                MemoryConflict.memory_id == memory.id,
                MemoryConflict.conflicting_memory_id == memory.id,
            )
        )
    )
    db.execute(
        update(MemoryAudit)
        .where(MemoryAudit.memory_id == memory.id)
        .values(payload_json=json.dumps({"redacted": action}))
    )
    deletion_receipt.record(db, agent_id, "memory", memory.id)
    db.add(MemoryAudit(action=action, memory_id=memory.id, payload_json=json.dumps(detail)))
    db.delete(memory)


def _view(row):
    return {
        "request_id": row.request_id,
        "memory_id": row.memory_id,
        "agent_id": row.agent_id,
        "revision": row.revision,
        "status": "forgotten",
        "index_removal": row.index_removal,
    }


def _prior(db, agent_id, request_id, digest=None):
    row = db.get(MemoryForget, (agent_id, request_id))
    if row is not None and digest is not None and row.payload_hash != digest:
        raise HTTPException(409, "Forget request identity already used")
    return row


def read_receipt(agent_id, request_id):
    with SessionLocal() as db:
        row = _prior(db, agent_id, request_id)
        if row is None:
            raise HTTPException(404, "Forget receipt not found")
        return _view(row)


def forget(agent_id, request_id, payload):
    digest = hashlib.sha256(
        json.dumps(
            {"agent_id": agent_id, **payload}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    try:
        with SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            prior = _prior(db, agent_id, request_id, digest)
            if prior is not None:
                return _view(prior)
            memory = db.execute(
                select(Memory)
                .where(Memory.id == payload["memory_id"], Memory.agent_id == agent_id)
                .with_for_update()
            ).scalar_one_or_none()
            if memory is None:
                raise HTTPException(404, "Memory not found")
            prior = _prior(db, agent_id, request_id, digest)
            if prior is not None:
                return _view(prior)
            if memory.revision != payload["expected_revision"]:
                raise HTTPException(409, "Memory revision changed; review the current record")
            receipt = MemoryForget(
                agent_id=agent_id,
                request_id=request_id,
                memory_id=memory.id,
                payload_hash=digest,
                revision=memory.revision,
                index_removal="pending",
            )
            erase(
                db,
                agent_id,
                memory,
                "owner_forget",
                {"request_id": request_id, "revision": memory.revision},
            )
            db.add(receipt)
            db.commit()
            result = _view(receipt)
    except IntegrityError:
        with SessionLocal() as db:
            prior = _prior(db, agent_id, request_id, digest)
            if prior is not None:
                return _view(prior)
        raise HTTPException(409, "Memory or forget request changed") from None

    removal = index_after_commit(delete_memory_embedding, payload["memory_id"])
    state = "removed" if removal.get("status") == "ok" else "degraded"
    try:
        with SessionLocal() as db:
            db.execute(
                update(MemoryForget)
                .where(MemoryForget.agent_id == agent_id, MemoryForget.request_id == request_id)
                .values(index_removal=state)
            )
            db.commit()
        result["index_removal"] = state
    except SQLAlchemyError:
        pass  # The forget is durable; the receipt stays visibly pending.
    return result
