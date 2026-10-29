"""Stated memories: one fact, stored word for word, with a durable receipt.

The owner's own words (typed, or approved as exact text) are a high-confidence user memory.
Words Conker wrote itself are kept as a medium-confidence assistant memory, so a reader
can always tell the two apart.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from app.models.memory import Memory
from app.models.memory_correction import MemoryCorrection
from app.services.classifier import classify_memory
from app.services.memory_truth import add_revision, detect_conflicts
from app.services.qdrant_store import upsert_memory_embedding
from app.services.vector_outbox import enqueue_upsert, reconcile_one
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

PHASE_REVIEW_WINDOW = timedelta(days=14)
# origin -> (source type, confidence, revision note, revision author, audit action)
ORIGINS = {
    "owner": ("user", "high", "owner-approved statement", "owner_review", "owner_stated_memory"),
    "conker": ("assistant", "medium", "Conker-written statement", "conker", "conker_stated_memory"),
}


def _reconcile_index_intent(operation, memory_id, *args, **kwargs):
    return reconcile_one("memory", memory_id, sessions=SessionLocal, operation_override=operation)


def _view(row, created):
    return {
        "request_id": row.request_id,
        "memory_id": row.memory_id,
        "agent_id": row.agent_id,
        "payload_hash": row.payload_hash,
        "revision": row.revision,
        "status": "remembered" if created else "already_remembered",
        "indexing": row.indexing,
    }


def _prior(db, agent_id, request_id, digest):
    row = db.get(MemoryCorrection, (agent_id, request_id))
    if row is not None and row.payload_hash != digest:
        raise HTTPException(409, "Request identity already used")
    return row


def remember(agent_id, request_id, statement, origin="owner"):
    """Store the text exactly as written, marked with who wrote it. An exact copy of an active
    memory is not stored twice; the receipt then names the existing memory."""
    source_type, confidence, reason, changed_by, action = ORIGINS[origin]
    request = {"agent_id": agent_id, "kind": "remember", "text": statement}
    if origin != "owner":
        request["origin"] = origin  # owner digests stay as before, so old receipts still replay
    digest = hashlib.sha256(
        json.dumps(
            request,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    try:
        with SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            prior = _prior(db, agent_id, request_id, digest)
            if prior is not None:
                return _view(prior, prior.previous_revision == 0)
            normalized = statement.strip().lower()
            existing = next(
                (
                    row
                    for row in db.execute(
                        select(Memory).where(Memory.agent_id == agent_id, Memory.status == "active")
                    ).scalars()
                    if row.text.strip().lower() == normalized
                ),
                None,
            )
            if existing is not None:
                receipt = MemoryCorrection(
                    agent_id=agent_id,
                    request_id=request_id,
                    memory_id=existing.id,
                    payload_hash=digest,
                    previous_revision=existing.revision,
                    revision=existing.revision,
                    indexing="indexed",
                )
                db.add(receipt)
                db.commit()
                return _view(receipt, False)
            # The classifier only picks the type; the stated words stay the text and the summary.
            kind = classify_memory(statement, source_type)["memory_type"]
            memory = Memory(
                agent_id=agent_id,
                text=statement,
                summary=statement,
                memory_type=kind,
                source_type=source_type,
                confidence=confidence,
                review_by=datetime.now(UTC) + PHASE_REVIEW_WINDOW if kind == "phase" else None,
                tags_json="[]",
                valid_from=datetime.now(UTC),
            )
            db.add(memory)
            db.flush()
            add_revision(db, memory, reason, changed_by)
            detect_conflicts(db, memory)
            receipt = MemoryCorrection(
                agent_id=agent_id,
                request_id=request_id,
                memory_id=memory.id,
                payload_hash=digest,
                previous_revision=0,
                revision=memory.revision,
                indexing="pending",
            )
            db.add(receipt)
            db.add(
                MemoryAudit(
                    action=action,
                    memory_id=memory.id,
                    payload_json=json.dumps({"request_id": request_id}),
                )
            )
            index_payload = {
                "agent_id": agent_id,
                "memory_type": kind,
                "source_type": source_type,
                "confidence": confidence,
                "tags": [],
            }
            enqueue_upsert(
                db, "memory", memory.id, statement, agent_id=agent_id, payload=index_payload
            )
            db.commit()
            result = _view(receipt, True)
    except IntegrityError:
        # A concurrent identical request won the receipt; this one rolled back entirely.
        with SessionLocal() as db:
            prior = _prior(db, agent_id, request_id, digest)
            if prior is not None:
                return _view(prior, prior.previous_revision == 0)
        raise HTTPException(409, "Request identity changed") from None

    # Indexing runs after the authoritative commit; a replay never repeats it.
    try:
        indexed = _reconcile_index_intent(
            upsert_memory_embedding, result["memory_id"], statement, payload=index_payload
        )
        state = "indexed" if indexed.get("status") == "ok" else "degraded"
        with SessionLocal() as db:
            db.execute(
                update(MemoryCorrection)
                .where(
                    MemoryCorrection.agent_id == agent_id,
                    MemoryCorrection.request_id == request_id,
                )
                .values(indexing=state)
            )
            db.commit()
        result["indexing"] = state
    except SQLAlchemyError:
        pass  # The receipt stays visibly pending; the memory is already stored.
    return result
