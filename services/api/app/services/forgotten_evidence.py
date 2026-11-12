"""Forgetting a memory takes the words it came from, unless they still back another memory.

Evidence backs a memory when lineage joins them: an analysis made from the evidence (named in
its evidence ids or linked from it) supports the memory, a link joins the evidence and the memory
directly, or Pi's conversation receipt for that evidence admitted the memory.

Inside the forget's transaction, each such evidence row that backs no other remaining memory:

- becomes a content-free tombstone (kept, so ids in receipts and jobs still resolve), marked
  invalidated, so the explorer, context retrieval and evidence search skip it;
- loses its processing jobs, so nothing can analyse it into a memory again;
- takes with it the analyses made from forgotten evidence alone (they copy its words) and every
  link to it or to them;
- blanks the episode title, summary and tags it gave a shared episode;
- gets a deletion receipt, so recovery replays the same erase into a restored database.

Evidence that still backs another memory is left exactly as it is. Pi's transcript is Pi's.
"""

import json
from datetime import UTC, datetime

from app.models import deletion_receipt
from app.models.analysis_object import AnalysisObject
from app.models.conversation_receipt import ConversationReceipt
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.processing_job import ProcessingJob
from app.services.lexical_retrieval import escape_like
from sqlalchemy import and_, delete, or_, select

# Recovery replays this kind through listener_forgetting.erase_evidence, which erases any
# evidence row by id (a listener event or a conversation statement alike).
EVIDENCE_KIND = "listener_evidence"
TITLE = "Forgotten memory evidence"
REASON = "Memory forgotten"


def original(evidence: EvidenceObject) -> tuple[set[str], set[str]]:
    """The titles and summaries an episode may have copied from this evidence."""
    try:
        content = json.loads(evidence.normalized_payload_json or "{}").get("content") or ""
    except (ValueError, AttributeError):
        content = ""
    titles = {value for value in (evidence.title, content[:120]) if value}
    summaries = {value for value in (evidence.summary, content[:280]) if value}
    return titles, summaries


def blank_episodes(
    db,
    agent_id: str,
    evidence_id: str,
    titles: set[str],
    summaries: set[str],
    tags: str,
    title: str,
) -> None:
    """Blank what an episode took from this evidence; an episode only it fed is blanked whole.

    Only the agent's own episodes: a link from another namespace never lets a forget reach them."""
    grouped = select(ObjectLink.target_id).where(
        ObjectLink.source_type == "evidence",
        ObjectLink.source_id == evidence_id,
        ObjectLink.target_type == "episode",
        ObjectLink.relationship == "grouped_into",
    )
    episodes = select(EpisodeObject).where(
        EpisodeObject.agent_id == agent_id, EpisodeObject.id.in_(grouped)
    )
    for episode in db.scalars(episodes):
        others = db.scalar(
            select(ObjectLink.id)
            .where(
                ObjectLink.target_type == "episode",
                ObjectLink.target_id == episode.id,
                ObjectLink.relationship == "grouped_into",
                ObjectLink.source_id != evidence_id,
            )
            .limit(1)
        )
        if others is None or episode.title in titles:
            episode.title = title
        if others is None or episode.summary in summaries:
            episode.summary = ""
        if others is None or episode.tags_json == tags:
            episode.tags_json = "[]"


def _evidence_ids(analysis: AnalysisObject) -> set[str]:
    try:
        values = json.loads(analysis.evidence_ids_json or "[]")
    except ValueError:
        return set()
    return (
        {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()
    )


def _analyses_from(db, agent_id: str, evidence_id: str) -> set[str]:
    member = '%"' + escape_like(evidence_id) + '"%'
    linked = select(ObjectLink.target_id).where(
        ObjectLink.source_type == "evidence",
        ObjectLink.source_id == evidence_id,
        ObjectLink.target_type == "analysis",
    )
    return set(
        db.scalars(
            select(AnalysisObject.id).where(
                AnalysisObject.agent_id == agent_id,
                or_(
                    AnalysisObject.evidence_ids_json.like(member, escape="/"),
                    AnalysisObject.id.in_(linked),
                ),
            )
        )
    )


def _direct(kind: str, identity: str, other: str):
    """Links joining this object to an object of kind `other`, in either direction."""
    return or_(
        and_(
            ObjectLink.source_type == kind,
            ObjectLink.source_id == identity,
            ObjectLink.target_type == other,
        ),
        and_(
            ObjectLink.target_type == kind,
            ObjectLink.target_id == identity,
            ObjectLink.source_type == other,
        ),
    )


def _far_end(link: ObjectLink, identity: str) -> str:
    return link.target_id if link.source_id == identity else link.source_id


def _memories_backed_by(db, agent_id: str, evidence_id: str) -> set[str]:
    memories = {
        _far_end(link, evidence_id)
        for link in db.scalars(select(ObjectLink).where(_direct("evidence", evidence_id, "memory")))
    }
    analyses = _analyses_from(db, agent_id, evidence_id)
    if analyses:
        memories.update(
            db.scalars(
                select(ObjectLink.target_id).where(
                    ObjectLink.source_type == "analysis",
                    ObjectLink.source_id.in_(analyses),
                    ObjectLink.target_type == "memory",
                    ObjectLink.relationship == "supports",
                )
            )
        )
    receipt = db.get(ConversationReceipt, evidence_id)
    if receipt is not None and receipt.agent_id == agent_id and receipt.memory_id:
        memories.add(receipt.memory_id)
    return memories


def _evidence_backing(db, agent_id: str, memory_id: str) -> set[str]:
    evidence = {
        _far_end(link, memory_id)
        for link in db.scalars(select(ObjectLink).where(_direct("memory", memory_id, "evidence")))
    }
    supporting = select(ObjectLink.source_id).where(
        ObjectLink.source_type == "analysis",
        ObjectLink.target_type == "memory",
        ObjectLink.target_id == memory_id,
        ObjectLink.relationship == "supports",
    )
    for analysis in db.scalars(
        select(AnalysisObject).where(
            AnalysisObject.agent_id == agent_id,
            AnalysisObject.id.in_(supporting),
        )
    ):
        evidence |= _evidence_ids(analysis)
        evidence.update(
            db.scalars(
                select(ObjectLink.source_id).where(
                    ObjectLink.source_type == "evidence",
                    ObjectLink.target_type == "analysis",
                    ObjectLink.target_id == analysis.id,
                )
            )
        )
    evidence.update(
        db.scalars(
            select(ConversationReceipt.id).where(
                ConversationReceipt.agent_id == agent_id,
                ConversationReceipt.memory_id == memory_id,
            )
        )
    )
    return evidence


def _backs_another(db, agent_id: str, evidence_id: str, memory_id: str) -> bool:
    others = _memories_backed_by(db, agent_id, evidence_id) - {memory_id}
    return (
        bool(others)
        and db.scalar(select(Memory.id).where(Memory.id.in_(others)).limit(1)) is not None
    )


def _tombstone(db, agent_id: str, evidence: EvidenceObject) -> None:
    deletion_receipt.record(db, agent_id, EVIDENCE_KIND, evidence.id)
    db.execute(delete(ProcessingJob).where(ProcessingJob.evidence_id == evidence.id))
    titles, summaries = original(evidence)
    blank_episodes(db, agent_id, evidence.id, titles, summaries, evidence.tags_json, TITLE)
    evidence.title = TITLE
    evidence.summary = ""
    evidence.raw_payload_json = json.dumps(
        {"evidence_id": evidence.id, "content_status": "forgotten"}
    )
    evidence.normalized_payload_json = "{}"
    evidence.tags_json = "[]"
    evidence.invalidated_at = evidence.invalidated_at or datetime.now(UTC)
    evidence.invalidation_reason = REASON
    evidence.processing_state = "invalidated"


def _analyses_to_drop(db, agent_id: str, forgotten: list[str]) -> list[str]:
    """Analyses made only from evidence forgotten now: they hold copies of its words."""
    gone = set(forgotten)
    candidates = set().union(*(_analyses_from(db, agent_id, identity) for identity in forgotten))
    drop = []
    for analysis in db.scalars(select(AnalysisObject).where(AnalysisObject.id.in_(candidates))):
        sources = _evidence_ids(analysis) | set(
            db.scalars(
                select(ObjectLink.source_id).where(
                    ObjectLink.source_type == "evidence",
                    ObjectLink.target_type == "analysis",
                    ObjectLink.target_id == analysis.id,
                )
            )
        )
        if sources and sources <= gone:
            drop.append(analysis.id)
    return sorted(drop)


def forget_sources(db, agent_id: str, memory_id: str) -> list[str]:
    """Tombstone the evidence only this memory used, inside the caller's transaction."""
    forgotten = []
    for identity in sorted(_evidence_backing(db, agent_id, memory_id)):
        evidence = db.execute(
            select(EvidenceObject).where(EvidenceObject.id == identity).with_for_update()
        ).scalar_one_or_none()
        if evidence is None or evidence.agent_id != agent_id:
            continue
        if _backs_another(db, agent_id, identity, memory_id):
            continue
        forgotten.append(identity)
    # Decide every row first: tombstoning drops links the other decisions still read.
    analyses = _analyses_to_drop(db, agent_id, forgotten)
    for identity in forgotten:
        _tombstone(db, agent_id, db.get(EvidenceObject, identity))
    owned = [*forgotten, *analyses]
    if owned:
        db.execute(
            delete(ObjectLink).where(
                or_(ObjectLink.source_id.in_(owned), ObjectLink.target_id.in_(owned))
            )
        )
    if analyses:
        db.execute(delete(AnalysisObject).where(AnalysisObject.id.in_(analyses)))
    return forgotten
