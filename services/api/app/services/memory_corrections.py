"""Namespace-bound CAS corrections with atomic audit, history and receipt."""

import hashlib
import json

from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from app.models.memory import Memory
from app.models.memory_correction import MemoryCorrection
from app.services.memory_truth import add_revision
from app.services.qdrant_store import index_after_commit, upsert_memory_embedding
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError


def _view(row):
    return {
        "request_id": row.request_id,
        "memory_id": row.memory_id,
        "agent_id": row.agent_id,
        "previous_revision": row.previous_revision,
        "revision": row.revision,
        "status": "applied",
        "indexing": row.indexing,
    }


def _prior(db, agent_id, request_id, digest=None):
    row = db.get(MemoryCorrection, (agent_id, request_id))
    if row is not None and digest is not None and row.payload_hash != digest:
        raise HTTPException(409, "Correction request identity already used")
    return row


def read_memory(agent_id, memory_id):
    with SessionLocal() as db:
        row = db.execute(
            select(Memory).where(Memory.id == memory_id, Memory.agent_id == agent_id)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "Memory not found")
        if len(row.text) > 16000 or any(
            len(v) > 100 for v in (row.source_type, row.confidence)
        ):
            raise HTTPException(409, "Memory exceeds correction transport bounds")
        return {
            "id": row.id,
            "agent_id": row.agent_id,
            "revision": row.revision,
            "text": row.text,
            "source_type": row.source_type,
            "confidence": row.confidence,
        }


def read_receipt(agent_id, request_id):
    with SessionLocal() as db:
        row = _prior(db, agent_id, request_id)
        if row is None:
            raise HTTPException(404, "Correction receipt not found")
        return _view(row)


def apply(agent_id, request_id, payload):
    digest = hashlib.sha256(
        json.dumps(
            {"agent_id": agent_id, **payload},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    try:
        with SessionLocal() as db:
            # SQLite needs a writer reservation; PostgreSQL uses the row lock below.
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
            # A concurrent identical request may have committed while the row lock waited.
            prior = _prior(db, agent_id, request_id, digest)
            if prior is not None:
                return _view(prior)
            if memory.revision != payload["expected_revision"]:
                raise HTTPException(
                    409, "Memory revision changed; review the current record"
                )
            previous = memory.revision
            add_revision(
                db, memory, "before reviewed runtime text correction", "owner_review"
            )
            memory.text = payload["text"]
            # Exact text is safe as a retrieval summary; no classifier or inferred upgrade.
            memory.summary = payload["text"]
            memory.revision = previous + 1
            receipt = MemoryCorrection(
                agent_id=agent_id,
                request_id=request_id,
                memory_id=memory.id,
                payload_hash=digest,
                previous_revision=previous,
                revision=previous + 1,
                indexing="pending",
            )
            db.add(receipt)
            db.add(
                MemoryAudit(
                    action="reviewed_text_correction",
                    memory_id=memory.id,
                    payload_json=json.dumps(
                        {
                            "request_id": request_id,
                            "previous_revision": previous,
                            "revision": previous + 1,
                        }
                    ),
                )
            )
            index_payload = {
                "agent_id": agent_id,
                "memory_type": memory.memory_type,
                "source_type": memory.source_type,
                "confidence": memory.confidence,
                "tags": json.loads(memory.tags_json),
            }
            db.commit()
            result = _view(receipt)
    except (IntegrityError, StaleDataError):
        # A different target can race for the same request ID. The entire losing edit rolled back.
        with SessionLocal() as db:
            prior = _prior(db, agent_id, request_id, digest)
            if prior is not None:
                return _view(prior)
        raise HTTPException(
            409, "Memory revision or correction request changed"
        ) from None

    # Derived vector indexing is outside the authoritative transaction. Replays never repeat it.
    # A crash here leaves a visible pending receipt, while the authoritative correction is durable.
    try:
        with SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            current = db.execute(
                select(Memory)
                .where(Memory.id == payload["memory_id"], Memory.agent_id == agent_id)
                .with_for_update()
            ).scalar_one_or_none()
            # Serialize correction indexing with subsequent SQL edits so a late older
            # correction cannot overwrite the latest correction's vector.
            state = "degraded"
            if current is not None and current.revision == result["revision"]:
                indexed = index_after_commit(
                    upsert_memory_embedding,
                    payload["memory_id"],
                    payload["text"],
                    payload=index_payload,
                )
                state = "indexed" if indexed.get("status") == "ok" else "degraded"
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
        pass  # Receipt remains visibly pending; the edit is already authoritative.
    return result
