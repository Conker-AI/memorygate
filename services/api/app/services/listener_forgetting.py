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
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.processing_job import ProcessingJob
from app.services import memory_forgetting
from app.services.qdrant_store import delete_memory_embedding
from sqlalchemy import delete, or_, select, text

KIND = "listener_evidence"
ACTION = "listener_evidence_erase"
TOMBSTONE_TITLE = "Forgotten listener event"
# Only memories the listener pipeline created are erased with their evidence.
DERIVED_SOURCE = "automatic_listener"


def _original(evidence: EvidenceObject) -> tuple[set[str], set[str]]:
    """The titles and summaries an episode may have copied from this evidence."""
    try:
        content = json.loads(evidence.normalized_payload_json or "{}").get("content") or ""
    except (ValueError, AttributeError):
        content = ""
    titles = {value for value in (evidence.title, content[:120]) if value}
    summaries = {value for value in (evidence.summary, content[:280]) if value}
    return titles, summaries


def _blank_evidence(evidence: EvidenceObject) -> None:
    evidence.title = TOMBSTONE_TITLE
    evidence.summary = ""
    evidence.raw_payload_json = json.dumps({"evidence_id": evidence.id, "content_status": "forgotten"})
    evidence.normalized_payload_json = "{}"
    evidence.tags_json = "[]"
    evidence.invalidated_at = evidence.invalidated_at or datetime.now(UTC)
    evidence.invalidation_reason = "Listener event erased"
    evidence.processing_state = "invalidated"


def _blank_episodes(db, evidence_id: str, titles: set[str], summaries: set[str]) -> None:
    grouped = select(ObjectLink.target_id).where(
        ObjectLink.source_type == "evidence", ObjectLink.source_id == evidence_id,
        ObjectLink.target_type == "episode", ObjectLink.relationship == "grouped_into",
    )
    for episode in db.scalars(select(EpisodeObject).where(EpisodeObject.id.in_(grouped))):
        others = db.scalar(select(ObjectLink.id).where(
            ObjectLink.target_type == "episode", ObjectLink.target_id == episode.id,
            ObjectLink.relationship == "grouped_into", ObjectLink.source_id != evidence_id,
        ).limit(1))
        if others is None or episode.title in titles:
            episode.title = TOMBSTONE_TITLE
        if others is None or episode.summary in summaries:
            episode.summary = ""
        if others is None:
            episode.tags_json = "[]"


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
    titles, summaries = _original(evidence)
    _blank_evidence(evidence)
    analysis_ids = list(db.scalars(select(AnalysisObject.id).where(
        AnalysisObject.agent_id == agent_id,
        AnalysisObject.evidence_ids_json == json.dumps([evidence_id]),
    )))
    memories = _memories_to_erase(db, analysis_ids)
    _blank_episodes(db, evidence_id, titles, summaries)
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
