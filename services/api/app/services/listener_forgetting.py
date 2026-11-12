"""Erasing one listener event removes its words and everything MemoryGate derived from them.

A listener event (for example a message Pi imported from another assistant's export) becomes
evidence, an episode grouping, one or two analyses, and maybe a memory. Erasing the evidence, in
one transaction:

- blanks the evidence row (kept as a content-free tombstone, so a repeat call is a no-op);
- deletes the analyses made from this evidence alone, and every link to it or to them;
- erases each automatic listener memory those analyses supported when nothing else supports it
  (a memory the owner wrote, or one another event still supports, keeps its text: only the link
  goes);
- blanks the episode's title, summary and tags when they came from this evidence (the episode is
  shared by events within 30 minutes, so it is never deleted here);
- deletes the processing jobs, so a running worker loses its lease and commits nothing, and
  nothing can retry the event.

A deletion receipt lets recovery replay the erase into a restored database. Vector points of
erased memories are queued for deletion in the same transaction and removed after commit.
"""

import contextlib
import json
from datetime import UTC, datetime

from app.core.db import SessionLocal
from app.models import deletion_receipt
from app.models.analysis_object import AnalysisObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.memory_revision import MemoryRevision
from app.models.object_link import ObjectLink
from app.models.processing_job import ProcessingJob
from app.services import memory_forgetting
from app.services.forgotten_evidence import blank_episodes, original
from app.services.memory_text_lock import lock_memory_text
from app.services.qdrant_store import delete_memory_embedding
from sqlalchemy import delete, or_, select, text

KIND = "listener_evidence"
ACTION = "listener_evidence_erase"
TOMBSTONE_TITLE = "Forgotten listener event"
# Only memories the listener pipeline created, and the owner never edited, corrected or chose
# in a conflict, are erased with their evidence.
DERIVED_SOURCE = "automatic_listener"
OWNER_CHANGES = ("user", "owner_review")


def _blank_evidence(evidence: EvidenceObject) -> None:
    evidence.title = TOMBSTONE_TITLE
    evidence.summary = ""
    evidence.raw_payload_json = json.dumps({"evidence_id": evidence.id, "content_status": "forgotten"})
    evidence.normalized_payload_json = "{}"
    evidence.tags_json = "[]"
    evidence.invalidated_at = evidence.invalidated_at or datetime.now(UTC)
    evidence.invalidation_reason = "Listener event erased"
    evidence.processing_state = "invalidated"


def _memories_to_erase(db, analysis_ids: list[str]) -> list[Memory]:
    if not analysis_ids:
        return []
    supported = db.scalars(select(ObjectLink.target_id).where(
        ObjectLink.source_type == "analysis", ObjectLink.source_id.in_(analysis_ids),
        ObjectLink.target_type == "memory", ObjectLink.relationship == "supports",
    )).all()
    erase = []
    for memory_id in sorted(set(supported)):
        memory = db.get(Memory, memory_id)
        if memory is None or memory.source_type != DERIVED_SOURCE:
            continue
        # The pipeline's lock for this text: a worker about to reuse this memory for another
        # event either links it before this check or finds it gone and makes its own.
        lock_memory_text(db, memory.agent_id, memory.text)
        owner_touched = db.scalar(select(MemoryRevision.id).where(
            MemoryRevision.memory_id == memory_id, MemoryRevision.changed_by.in_(OWNER_CHANGES),
        ).limit(1))
        if owner_touched is not None:
            continue  # the owner corrected or chose it: their words now, only the link goes
        other_support = db.scalar(select(ObjectLink.id).where(
            ObjectLink.target_type == "memory", ObjectLink.target_id == memory_id,
            ObjectLink.relationship == "supports", ObjectLink.confidence > 0,
            ObjectLink.source_id.not_in(analysis_ids),
        ).limit(1))
        if other_support is None:
            erase.append(memory)
    return erase


def erase_in_session(db, agent_id: str, evidence: EvidenceObject | None, evidence_id: str) -> list[str]:
    """Erase inside the caller's transaction; the ids of the memories erased with it."""
    deletion_receipt.record(db, agent_id, KIND, evidence_id)
    # Jobs first: on PostgreSQL this waits for a worker holding the row, and a worker that
    # comes later finds no job to fence and commits nothing.
    db.execute(delete(ProcessingJob).where(ProcessingJob.evidence_id == evidence_id))
    if evidence is None:
        return []
    titles, summaries = original(evidence)
    tags = evidence.tags_json
    _blank_evidence(evidence)
    analysis_ids = list(db.scalars(select(AnalysisObject.id).where(
        AnalysisObject.agent_id == agent_id,
        AnalysisObject.evidence_ids_json == json.dumps([evidence_id]),
    )))
    memories = _memories_to_erase(db, analysis_ids)
    blank_episodes(db, agent_id, evidence_id, titles, summaries, tags, TOMBSTONE_TITLE)
    owned = [evidence_id, *analysis_ids]
    db.execute(delete(ObjectLink).where(
        or_(ObjectLink.source_id.in_(owned), ObjectLink.target_id.in_(owned))
    ))
    if analysis_ids:
        db.execute(delete(AnalysisObject).where(AnalysisObject.id.in_(analysis_ids)))
    erased = []
    for memory in memories:
        db.execute(delete(ObjectLink).where(
            or_(ObjectLink.source_id == memory.id, ObjectLink.target_id == memory.id)
        ))
        memory_forgetting.erase(db, agent_id, memory, ACTION, {"evidence_id": evidence_id})
        erased.append(memory.id)
    return erased


def erase_evidence(agent_id: str, evidence_id: str, *, source_id: str | None = None,
                   sessions=None) -> dict | None:
    """Erase one evidence row of this agent (and source, when given).

    None when the evidence does not exist for that agent and source; a route answers 404.
    With `sessions` (recovery replay into an offline database) a missing row is still
    recorded, and the live vector index is not touched: replay reports the ids instead.
    """
    with (sessions or SessionLocal)() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        evidence = db.execute(
            select(EvidenceObject).where(EvidenceObject.id == evidence_id).with_for_update()
        ).scalar_one_or_none()
        if evidence is not None and evidence.agent_id != agent_id:
            if sessions is not None:
                raise ValueError("Restored evidence namespace conflicts with deletion evidence.")
            evidence = None
        if evidence is not None and source_id is not None and evidence.source_id != source_id:
            evidence = None
        if evidence is None and sessions is None:
            return None
        erased = erase_in_session(db, agent_id, evidence, evidence_id)
        db.commit()
    if sessions is None:
        # The delete intent is durable; the outbox worker retries a removal that fails here.
        for memory_id in erased:
            with contextlib.suppress(Exception):
                memory_forgetting._reconcile_index_intent(delete_memory_embedding, memory_id)
    return {"status": "erased", "evidence_id": evidence_id, "memory_ids": erased}
