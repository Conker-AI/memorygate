"""Admin-only destructive reset operations for the single MemoryGate workspace."""
import json
from datetime import UTC, datetime
from itertools import islice
from uuid import NAMESPACE_URL, uuid5

from app.models.analysis_object import AnalysisObject
from app.models.audit import MemoryAudit
from app.models.conversation_receipt import ConversationReceipt
from app.models.entity import Entity, EntityEdge, EntityEvent, EntityHistory
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_correction import MemoryCorrection
from app.models.memory_revision import MemoryRevision
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.models.processing_job import ProcessingJob
from app.models.session_transcript import SessionTranscript
from app.models.vector_outbox import VectorOutbox
from app.services.backup_service import create_backup
from app.services.lineage_forgetting import _without
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

# Ids per statement: far below SQLite's and Postgres's bind-parameter limits, so a reset of any size
# costs a number of statements that grows with the data in steps of CHUNK, never one per row.
CHUNK = 500
VECTOR_KINDS = ("memory", "entity", "observation")

# What the reset removes, one entry per table. tests/test_memory_reset_coverage.py requires these
# sets and the kept ones there to cover every table, so a table added later cannot be skipped by
# accident. vector_outbox is rewritten (a delete intent per removed memory, entity and
# observation), not emptied.
RESET_TABLES = frozenset({
    "memories", "entities", "observations", "patterns", "evidence_objects", "analysis_objects",
    "episode_objects", "session_transcripts", "processing_jobs", "memory_audit",
    "conversation_receipts", "memory_corrections", "memory_revisions", "memory_conflicts",
    "entity_history", "entity_events", "entity_edges", "object_links",
})
REWRITTEN_TABLES = frozenset({"vector_outbox"})


def _chunks(values, size: int = CHUNK):
    iterator = iter(values)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _ids(db, model, reset_from: datetime | None, *extra):
    """(id, *extra columns) of the rows the reset takes, as plain tuples (no ORM objects)."""
    query = select(model.id, *(getattr(model, name) for name in extra))
    if reset_from is not None:
        if not hasattr(model, "created_at"):
            return []
        query = query.where(model.created_at >= reset_from)
    return [tuple(row) for row in db.execute(query)]


def _delete_in(db, model, column, values) -> int:
    removed = 0
    for chunk in _chunks(values):
        removed += db.execute(delete(model).where(column.in_(chunk))).rowcount or 0
    return removed


def _enqueue_deletes(db, kind: str, rows: list[tuple[str, str]]) -> None:
    """Record a vector delete intent per row, as vector_outbox.enqueue_delete does, but with one
    multi-row statement per chunk (the generation counter still rises on a repeat)."""
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    now = datetime.now(UTC)
    for chunk in _chunks(rows, 50):
        values = [{
            "id": str(uuid5(NAMESPACE_URL, f"memorygate-vector:{kind}:{object_id}")),
            "agent_id": agent_id, "object_kind": kind, "object_id": object_id,
            "operation": "delete", "document_text": None, "payload_json": "{}", "generation": 1,
            "status": "pending", "attempts": 0, "next_attempt_at": now, "last_error": "",
        } for object_id, agent_id in chunk]
        statement = insert(VectorOutbox).values(values)
        db.execute(statement.on_conflict_do_update(
            index_elements=["object_kind", "object_id"],
            set_={
                "agent_id": statement.excluded.agent_id, "operation": statement.excluded.operation,
                "document_text": statement.excluded.document_text,
                "payload_json": statement.excluded.payload_json,
                "generation": VectorOutbox.generation + 1, "status": "pending", "attempts": 0,
                "next_attempt_at": statement.excluded.next_attempt_at, "last_error": "",
                "updated_at": now,
            },
        ))


def _strip_entities(db, table: str, column: str, entity_ids: set[str]) -> None:
    """Take every removed entity id out of the kept rows' JSON id lists: one scan and one UPDATE
    per changed row (the old form scanned the table once per removed entity). `table` and
    `column` are constants."""
    rows = db.execute(text(f"SELECT id, {column} AS ids FROM {table} WHERE {column} IS NOT NULL")).fetchall()
    for row in rows:
        kept = row.ids
        for entity_id in entity_ids:
            if entity_id in kept:
                left = _without(kept, entity_id)
                if left is not None:
                    kept = json.dumps(left)
        if kept != row.ids:
            db.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": kept, "id": row.id})


def reset_memory(db, reset_from: datetime | None = None) -> dict:
    """Create a logical backup, then remove knowledge data and its vector points.

    Evidence-source configuration and all access credentials deliberately survive.
    """
    backup = create_backup(db)
    plan = (
        ("memories", Memory), ("entities", Entity), ("observations", Observation), ("patterns", Pattern),
        ("evidence", EvidenceObject), ("analysis", AnalysisObject), ("episodes", EpisodeObject),
        ("transcripts", SessionTranscript), ("jobs", ProcessingJob), ("audit", MemoryAudit),
    )
    selected = {
        name: _ids(db, model, reset_from, *(("agent_id",) if model in (Memory, Entity, Observation) else ()))
        for name, model in plan
    }
    memory_ids = [row[0] for row in selected["memories"]]
    entity_ids = [row[0] for row in selected["entities"]]
    for kind, name in zip(VECTOR_KINDS, ("memories", "entities", "observations"), strict=True):
        _enqueue_deletes(db, kind, selected[name])
    all_selected_ids = {row[0] for rows in selected.values() for row in rows}

    # A conversation receipt is the evidence's identity (same id) and names its memory. Once the
    # reset removes either, a surviving receipt would answer a redelivered message with a memory
    # that no longer exists. Forgetting tombstones stay: a forgotten message must never return.
    receipt_ids: set = set()
    for chunk in _chunks([row[0] for row in selected["evidence"]] + memory_ids):
        receipt_ids.update(db.scalars(select(ConversationReceipt.id).where(
            ConversationReceipt.state != "deleted",
            ConversationReceipt.id.in_(chunk) | ConversationReceipt.memory_id.in_(chunk))))
    receipts = _delete_in(db, ConversationReceipt, ConversationReceipt.id, receipt_ids)

    # A correction receipt replays its answer for a repeated request id, so one naming a removed
    # memory would report a correction of a memory that no longer exists. Forget receipts stay
    # (tombstones), like the forgetting conversation receipts above.
    corrections = _delete_in(db, MemoryCorrection, MemoryCorrection.memory_id, memory_ids)

    # A dated reset keeps older observations and patterns; they must not name removed entities.
    if reset_from is not None and entity_ids:
        _strip_entities(db, "observations", "entity_ids_json", set(entity_ids))
        _strip_entities(db, "patterns", "applies_to_entity_ids_json", set(entity_ids))

    # Linked history must not survive as orphaned records after a reset.
    for model, field in ((MemoryRevision, "memory_id"), (MemoryConflict, "memory_id"), (MemoryConflict, "conflicting_memory_id"),
                         (EntityHistory, "entity_id"), (EntityEvent, "entity_id"), (EntityEdge, "from_entity_id"),
                         (EntityEdge, "to_entity_id")):
        _delete_in(db, model, getattr(model, field), all_selected_ids)
    for chunk in _chunks(all_selected_ids):
        db.execute(delete(ObjectLink).where(ObjectLink.source_id.in_(chunk) | ObjectLink.target_id.in_(chunk)))

    for name, model in plan:
        _delete_in(db, model, model.id, [row[0] for row in selected[name]])
    db.commit()
    db.expire_all()
    removed = {name: len(rows) for name, rows in selected.items()}
    removed["receipts"] = receipts
    removed["corrections"] = corrections
    return {
        "backup": backup,
        "removed": removed,
        "reset_from": reset_from.isoformat() if reset_from else None,
        "indexing": {"status": "pending", "operations": sum(len(selected[n]) for n in ("memories", "entities", "observations"))},
    }
