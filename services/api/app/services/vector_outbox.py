"""Transactional desired-state outbox for the derived Qdrant index."""

import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from app.core.db import SessionLocal
from app.models.vector_outbox import VectorOutbox
from app.services import qdrant_store
from app.services.embeddings import EmbeddingUnavailable
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

KINDS = {"memory", "observation", "entity"}
OPERATIONS = {"upsert", "delete"}


def _identity(object_kind: str, object_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"memorygate-vector:{object_kind}:{object_id}"))


def enqueue(
    db,
    *,
    agent_id: str,
    object_kind: str,
    object_id: str,
    operation: str,
    document_text: str | None = None,
    payload: dict | None = None,
) -> int:
    """Record the latest desired vector state inside the caller's transaction."""
    if object_kind not in KINDS or operation not in OPERATIONS:
        raise ValueError("unsupported vector outbox intent")
    if operation == "upsert" and document_text is None:
        raise ValueError("vector upserts require document text")

    values = {
        "id": _identity(object_kind, object_id),
        "agent_id": agent_id,
        "object_kind": object_kind,
        "object_id": object_id,
        "operation": operation,
        "document_text": document_text,
        "payload_json": json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
        "generation": 1,
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": datetime.now(UTC),
        "last_error": "",
    }
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    statement = insert(VectorOutbox).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["object_kind", "object_id"],
        set_={
            "agent_id": statement.excluded.agent_id,
            "operation": statement.excluded.operation,
            "document_text": statement.excluded.document_text,
            "payload_json": statement.excluded.payload_json,
            "generation": VectorOutbox.generation + 1,
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": statement.excluded.next_attempt_at,
            "last_error": "",
            "updated_at": datetime.now(UTC),
        },
    )
    db.execute(statement)
    return db.execute(
        select(VectorOutbox.generation).where(
            VectorOutbox.object_kind == object_kind,
            VectorOutbox.object_id == object_id,
        )
    ).scalar_one()


def enqueue_upsert(db, object_kind: str, object_id: str, text_value: str, *, agent_id: str, payload: dict) -> int:
    return enqueue(
        db,
        agent_id=agent_id,
        object_kind=object_kind,
        object_id=object_id,
        operation="upsert",
        document_text=text_value,
        payload=payload,
    )


def enqueue_delete(db, object_kind: str, object_id: str, *, agent_id: str) -> int:
    return enqueue(
        db,
        agent_id=agent_id,
        object_kind=object_kind,
        object_id=object_id,
        operation="delete",
    )


def _apply(row: VectorOutbox, operation_override=None) -> None:
    if operation_override is not None:
        if row.operation == "delete":
            operation_override(row.object_id)
        else:
            operation_override(
                row.object_id,
                row.document_text,
                payload=json.loads(row.payload_json),
            )
        return
    operations = {
        ("memory", "upsert"): qdrant_store.upsert_memory_embedding,
        ("memory", "delete"): qdrant_store.delete_memory_embedding,
        ("observation", "upsert"): qdrant_store.upsert_observation_embedding,
        ("observation", "delete"): qdrant_store.delete_observation_embedding,
        ("entity", "upsert"): qdrant_store.upsert_entity_embedding,
        ("entity", "delete"): qdrant_store.delete_entity_embedding,
    }
    operation = operations[(row.object_kind, row.operation)]
    if row.operation == "delete":
        operation(row.object_id)
    else:
        operation(row.object_id, row.document_text, payload=json.loads(row.payload_json))


def _degraded(exc: Exception) -> dict:
    if isinstance(exc, EmbeddingUnavailable):
        return {"status": "degraded", "component": "embeddings", "reason": exc.reason}
    return {
        "status": "degraded",
        "component": "vector_index",
        "reason": qdrant_store.INDEX_UNREACHABLE,
    }


def _lock_next(db, *, object_kind: str | None = None, object_id: str | None = None):
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    now = datetime.now(UTC)
    statement = select(VectorOutbox).where(
        VectorOutbox.status == "pending",
        VectorOutbox.next_attempt_at <= now,
    )
    if object_kind is not None:
        statement = statement.where(VectorOutbox.object_kind == object_kind)
    if object_id is not None:
        statement = statement.where(VectorOutbox.object_id == object_id)
    return db.execute(
        statement.order_by(VectorOutbox.next_attempt_at, VectorOutbox.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def reconcile_one(
    object_kind: str,
    object_id: str,
    *,
    sessions=None,
    operation_override=None,
) -> dict:
    """Apply one current intent while holding its row lock through the effect."""
    with (sessions or SessionLocal)() as db:
        row = _lock_next(db, object_kind=object_kind, object_id=object_id)
        if row is None:
            current = db.execute(
                select(VectorOutbox).where(
                    VectorOutbox.object_kind == object_kind,
                    VectorOutbox.object_id == object_id,
                )
            ).scalar_one_or_none()
            db.rollback()
            if current is None or current.status == "complete":
                return {"status": "ok"}
            return {
                "status": "degraded",
                "component": "vector_index",
                "reason": "vector synchronization pending",
            }
        row.attempts += 1
        try:
            _apply(row, operation_override)
        except Exception as exc:
            result = _degraded(exc)
            row.last_error = f"{type(exc).__name__}: {result['reason']}"
            row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
            db.commit()
            return result
        row.status = "complete"
        row.last_error = ""
        row.next_attempt_at = datetime.now(UTC)
        db.commit()
        return {"status": "ok"}


def reconcile_pending(limit: int = 10, *, sessions=None) -> int:
    completed = 0
    for _ in range(max(0, limit)):
        with (sessions or SessionLocal)() as db:
            row = _lock_next(db)
            if row is None:
                db.rollback()
                break
            row.attempts += 1
            try:
                _apply(row)
            except Exception as exc:
                result = _degraded(exc)
                row.last_error = f"{type(exc).__name__}: {result['reason']}"
                row.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 2 ** min(row.attempts, 8))
                )
            else:
                row.status = "complete"
                row.last_error = ""
                row.next_attempt_at = datetime.now(UTC)
                completed += 1
            db.commit()
    return completed
