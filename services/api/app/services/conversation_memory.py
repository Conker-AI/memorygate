"""Idempotent owner evidence admission and tombstones for Pi's durable outbox.

No model output is promoted here. A memory is a quoted owner statement with a
citation and medium confidence, not an inferred fact about the owner.
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.core.db import SessionLocal
from app.models.analysis_object import AnalysisObject
from app.models.audit import MemoryAudit
from app.models.conversation_receipt import ConversationReceipt
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from app.models.object_link import ObjectLink
from app.services import qdrant_store
from app.services.signal_filter import score_value
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def identity(agent_id: str, message_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"pi:{agent_id}:{message_id}"))


def _lock(db, agent_id: str, message_id: str) -> ConversationReceipt:
    # PostgreSQL serializes one source ID. SQLite's test/runtime alternative
    # must acquire its writer lock before inspecting the same ID.
    sqlite = db.get_bind().dialect.name == "sqlite"
    if sqlite:
        db.execute(text("BEGIN IMMEDIATE"))
    insert = sqlite_insert if sqlite else pg_insert
    receipt_id = identity(agent_id, message_id)
    db.execute(
        insert(ConversationReceipt)
        .values(
            id=receipt_id,
            agent_id=agent_id,
            message_id=message_id,
            session_id="",
            state="new",
            value_score=0.0,
            index_pending="none",
            index_error="",
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return db.execute(
        select(ConversationReceipt).where(ConversationReceipt.id == receipt_id).with_for_update()
    ).scalar_one()


def _response(receipt: ConversationReceipt) -> dict:
    return {
        "id": receipt.id,
        "message_id": receipt.message_id,
        "state": receipt.state,
        "memory_id": receipt.memory_id,
        "value_score": receipt.value_score,
        "index_pending": receipt.index_pending,
        "index_error": receipt.index_error,
        "citation": {
            "message_id": receipt.message_id,
            "content_status": "forgotten" if receipt.state == "deleted" else "available",
        },
    }


def ingest(agent_id: str, message_id: str, payload: dict) -> dict:
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    with SessionLocal() as db:
        receipt = _lock(db, agent_id, message_id)
        if receipt.state == "deleted":
            db.commit()
            return _response(receipt)
        if receipt.fingerprint:
            if receipt.fingerprint != fingerprint:
                raise HTTPException(
                    409,
                    "Message identity already contains different evidence; reconcile Pi's outbox",
                )
            db.commit()
            return _response(receipt)
        content = payload["content"]
        occurred = datetime.fromtimestamp(payload["created_at"], timezone.utc)
        score = score_value(content)
        receipt.session_id = payload["session_id"]
        receipt.fingerprint = fingerprint
        receipt.value_score = score
        receipt.state = "admitted" if score >= 0.3 else "filtered"
        evidence = EvidenceObject(
            id=receipt.id,
            agent_id=agent_id,
            source_id="pi-conversation",
            source_key="pi-conversation",
            source_type="owner_statement",
            title="Owner conversation evidence",
            summary=content[:500],
            raw_payload_json=json.dumps(
                {"message_id": message_id, "session_id": payload["session_id"]}
            ),
            normalized_payload_json=json.dumps({"content": content}),
            occurred_at=occurred,
            integrity_confidence=1.0,
            processing_state="processed",
        )
        db.add(evidence)
        analysis_id = str(uuid5(NAMESPACE_URL, receipt.id + ":analysis"))
        db.add(
            AnalysisObject(
                id=analysis_id,
                agent_id=agent_id,
                analysis_type="owner_statement_admission",
                evidence_ids_json=json.dumps([receipt.id]),
                input_summary="Quoted owner statement",
                output_summary=f"Admission value {score:.2f}; no inference or generalization.",
                steps_json=json.dumps(["unicode_signal_filter", "retain_owner_attribution"]),
                confidence=0.5,
            )
        )
        db.add(
            ObjectLink(
                source_type="evidence",
                source_id=receipt.id,
                target_type="analysis",
                target_id=analysis_id,
                relationship="evaluated_by",
                created_by="pi-ingest",
            )
        )
        if receipt.state == "admitted":
            memory_id = str(uuid5(NAMESPACE_URL, receipt.id + ":memory"))
            receipt.memory_id = memory_id
            receipt.index_pending = "upsert"
            db.add(
                Memory(
                    id=memory_id,
                    agent_id=agent_id,
                    text=content,
                    summary=content[:280],
                    memory_type="context",
                    source_type="owner_statement",
                    confidence="medium",
                    do_not_generalize=True,
                    valid_from=occurred,
                )
            )
            db.add(
                ObjectLink(
                    source_type="analysis",
                    source_id=analysis_id,
                    target_type="memory",
                    target_id=memory_id,
                    relationship="supports",
                    created_by="pi-ingest",
                )
            )
        db.commit()
        return _response(receipt)


def forget(agent_id: str, message_id: str) -> dict:
    with SessionLocal() as db:
        receipt = _lock(db, agent_id, message_id)
        if receipt.state != "deleted":
            from app.models.deletion_receipt import record
            record(db, agent_id, "conversation", message_id)
            evidence = db.get(EvidenceObject, receipt.id)
            if evidence:
                evidence.title = "Forgotten conversation evidence"
                evidence.summary = ""
                evidence.raw_payload_json = json.dumps(
                    {"message_id": message_id, "content_status": "forgotten"}
                )
                evidence.normalized_payload_json = "{}"
                evidence.invalidated_at = datetime.now(timezone.utc)
                evidence.invalidation_reason = "Pi owner forgetting"
                evidence.processing_state = "invalidated"
            analysis_id = str(uuid5(NAMESPACE_URL, receipt.id + ":analysis"))
            db.execute(delete(AnalysisObject).where(AnalysisObject.id == analysis_id))
            if receipt.memory_id:
                db.execute(
                    delete(MemoryRevision).where(MemoryRevision.memory_id == receipt.memory_id)
                )
                db.execute(
                    delete(MemoryConflict).where(
                        (MemoryConflict.memory_id == receipt.memory_id)
                        | (MemoryConflict.conflicting_memory_id == receipt.memory_id)
                    )
                )
                for audit in db.scalars(
                    select(MemoryAudit).where(MemoryAudit.memory_id == receipt.memory_id)
                ):
                    audit.payload_json = '{"content_status":"forgotten"}'
                db.execute(
                    delete(Memory).where(
                        Memory.id == receipt.memory_id, Memory.agent_id == agent_id
                    )
                )
                receipt.index_pending = "delete"
                receipt.index_next_at = 0.0
            owned = [receipt.id, analysis_id, receipt.memory_id]
            db.execute(
                delete(ObjectLink).where(
                    ObjectLink.source_id.in_(owned) | ObjectLink.target_id.in_(owned)
                )
            )
            receipt.state = "deleted"
            receipt.fingerprint = None
            receipt.value_score = 0.0
            receipt.index_error = ""
        db.commit()
        return _response(receipt)


def index_pending(limit: int = 10) -> int:
    """Retry derived index writes by stable ID; tombstones serialize against upserts."""
    with SessionLocal() as db:
        ids = list(
            db.execute(
                select(ConversationReceipt.id)
                .where(
                    ConversationReceipt.index_pending != "none",
                    ConversationReceipt.index_next_at <= time.time(),
                )
                .order_by(ConversationReceipt.index_next_at, ConversationReceipt.id)
                .limit(limit)
            ).scalars()
        )
    completed = 0
    for receipt_id in ids:
        with SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            receipt = db.execute(
                select(ConversationReceipt)
                .where(ConversationReceipt.id == receipt_id)
                .with_for_update()
            ).scalar_one_or_none()
            if not receipt or receipt.index_pending == "none":
                continue
            try:
                if receipt.index_pending == "delete":
                    qdrant_store.delete_memory_embedding(receipt.memory_id)
                else:
                    memory = db.get(Memory, receipt.memory_id)
                    if memory is None or memory.status != "active":
                        receipt.index_pending = "delete"
                        db.commit()
                        continue
                    qdrant_store.upsert_memory_embedding(
                        memory.id,
                        memory.text,
                        payload={"agent_id": receipt.agent_id, "memory_type": memory.memory_type},
                    )
                receipt.index_pending = "none"
                receipt.index_error = ""
                completed += 1
            except Exception as exc:
                receipt.index_error = type(exc).__name__ + ": vector synchronization pending"
                receipt.index_next_at = time.time() + 30
            db.commit()
    return completed


def citations(db, memories: list[dict], agent_id: str) -> None:
    ids = [memory["id"] for memory in memories]
    receipts = {
        row.memory_id: row
        for row in db.execute(
            select(ConversationReceipt).where(
                ConversationReceipt.agent_id == agent_id, ConversationReceipt.memory_id.in_(ids)
            )
        ).scalars()
    }
    memories[:] = [
        memory
        for memory in memories
        if not receipts.get(memory["id"]) or receipts[memory["id"]].state != "deleted"
    ]
    for memory in memories:
        receipt = receipts.get(memory["id"])
        if receipt:
            memory["citations"] = [
                {
                    "message_id": receipt.message_id,
                    "session_id": receipt.session_id,
                    "content_status": "forgotten" if receipt.state == "deleted" else "available",
                }
            ]
