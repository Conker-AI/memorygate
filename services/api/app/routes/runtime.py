import hmac
import json
from datetime import UTC, datetime

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.auth import require_key
from app.core.db import SessionLocal
from app.core.timestamps import parse_timestamp
from app.models.evidence_object import EvidenceObject
from app.models.evidence_source import EvidenceSource
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.processing_job import ProcessingJob
from app.schemas.runtime import IngestEventRequest
from app.services import listener_forgetting
from app.services.auth_settings_service import (
    attempt,
)
from app.services.memory_truth import mark_unsupported
from app.services.processing_worker import requeue
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select

router = APIRouter(prefix="/runtime", tags=["runtime"])


def _enqueue(db, source: EvidenceSource, payload: IngestEventRequest, agent_id: str) -> dict:
    content = (
        payload.content.strip()
        or payload.title.strip()
        or json.dumps(payload.payload, ensure_ascii=True)
    )
    if not content:
        raise HTTPException(422, "Event must contain content, a title, or a payload")
    row = EvidenceObject(
        agent_id=agent_id,
        source_id=source.id,
        source_key=source.source_key,
        source_type=source.source_type,
        title=payload.title.strip() or content[:120],
        summary=content[:500],
        raw_payload_json=json.dumps(payload.payload),
        normalized_payload_json=json.dumps({"content": content}),
        tags_json=json.dumps(payload.tags),
        integrity_confidence=payload.integrity_confidence,
        processing_state="pending",
        occurred_at=parse_timestamp(payload.occurred_at, "occurred_at") or datetime.now(UTC),
    )
    db.add(row)
    db.flush()  # the job below needs the evidence id; both commit together
    source.last_ingested_at = row.occurred_at
    job = None
    if payload.auto_process:
        job = ProcessingJob(agent_id=agent_id, evidence_id=row.id, status="pending", stage="queued")
        db.add(job)
    db.commit()
    db.refresh(row)
    if job:
        db.refresh(job)
    return {
        "status": "queued" if job else "accepted",
        "evidence_id": row.id,
        "job_id": job.id if job else None,
    }


@router.post("/ingest")
def ingest(
    payload: IngestEventRequest,
    header_agent_id: str = Depends(get_agent_id),
    _: str = Depends(require_key),
):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        source = db.execute(
            select(EvidenceSource).where(
                EvidenceSource.source_key == payload.source_key.strip().lower()
            )
        ).scalar_one_or_none()
        if not source or not source.enabled:
            raise HTTPException(404, "Enabled evidence source not found")
        return _enqueue(db, source, payload, agent_id)
    finally:
        db.close()


def _listener_source(
    db, source_key: str, request: Request, key: str | None, *, require_enabled: bool = True
) -> tuple[EvidenceSource, str]:
    """The listener a source key names, checked against its own key; and its agent id."""
    source = db.execute(
        select(EvidenceSource).where(EvidenceSource.source_key == source_key.strip().lower())
    ).scalar_one_or_none()
    if not source or (require_enabled and not source.enabled):
        raise HTTPException(
            404,
            "Enabled evidence source not found" if require_enabled else "Evidence source not found",
        )
    client_host = request.client.host if request.client else "unknown"
    lock_scope = f"listener:{client_host}:{source.source_key}"
    secret = json.loads(source.secret_json or "{}").get("ingest_key", "")
    # Bytes, not str: compare_digest raises on a non-ASCII str, and a header can carry one.
    valid = bool(secret and key and hmac.compare_digest(secret.encode(), key.encode()))
    remaining = attempt(lock_scope, valid)
    if remaining:
        raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
    if not valid:
        raise HTTPException(401, "invalid listener key")
    return source, json.loads(source.config_json or "{}").get("agent_id", "default")


@router.post("/listeners/{source_key}")
def listener_ingest(
    source_key: str,
    payload: IngestEventRequest,
    request: Request,
    x_memorygate_listener_key: str | None = Header(None, alias="X-MemoryGate-Listener-Key"),
):
    """Dedicated listener ingress: source token only, never an admin credential."""
    db = SessionLocal()
    try:
        source, agent_id = _listener_source(db, source_key, request, x_memorygate_listener_key)
        payload.source_key = source.source_key
        payload.agent_id = agent_id
        return _enqueue(db, source, payload, payload.agent_id)
    finally:
        db.close()


@router.delete("/listeners/{source_key}/evidence/{evidence_id}")
def listener_erase(
    source_key: str,
    evidence_id: str,
    request: Request,
    x_memorygate_listener_key: str | None = Header(None, alias="X-MemoryGate-Listener-Key"),
):
    """Erase one event this listener sent and what was derived from it. Works on a disabled source."""
    db = SessionLocal()
    try:
        source, agent_id = _listener_source(
            db, source_key, request, x_memorygate_listener_key, require_enabled=False
        )
    finally:
        db.close()
    result = listener_forgetting.erase_evidence(agent_id, evidence_id, source_id=source.id)
    # Not 404: that answer means the listener itself is unknown, and its sender pauses on it.
    # Another listener's event gets the same answer as one that never existed.
    return result or {"status": "not_found", "evidence_id": evidence_id, "memory_ids": []}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, agent_id: str = Depends(get_agent_id), _: str = Depends(require_key)):
    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        if not job or job.agent_id != agent_id:
            raise HTTPException(404, "Processing job not found")
        evidence = db.get(EvidenceObject, job.evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence no longer exists")
        if not requeue(db, job.id):
            raise HTTPException(409, "Processing job is running; retry after it ends")
        return {"status": "queued", "job_id": job.id}
    finally:
        db.close()


@router.post("/evidence/{evidence_id}/invalidate")
def invalidate_evidence(
    evidence_id: str,
    reason: str,
    agent_id: str = Depends(get_agent_id),
    _: str = Depends(require_key),
):
    db = SessionLocal()
    try:
        evidence = db.get(EvidenceObject, evidence_id)
        if not evidence or evidence.agent_id != agent_id:
            raise HTTPException(404, "Evidence not found")
        evidence.invalidated_at = datetime.now(UTC)
        evidence.invalidation_reason = reason
        evidence.processing_state = "invalidated"
        links = (
            db.execute(
                select(ObjectLink).where(
                    ObjectLink.source_type == "evidence", ObjectLink.source_id == evidence_id
                )
            )
            .scalars()
            .all()
        )
        for link in links:
            link.confidence = 0.0
            link.metadata_json = json.dumps({"invalidated": True, "reason": reason})
        episode_ids = [link.target_id for link in links if link.target_type == "episode"]
        analysis_links = (
            db.execute(
                select(ObjectLink).where(
                    ObjectLink.source_type == "episode",
                    ObjectLink.source_id.in_(episode_ids),
                    ObjectLink.target_type == "analysis",
                )
            )
            .scalars()
            .all()
            if episode_ids
            else []
        )
        affected_analysis_ids = [link.target_id for link in analysis_links]
        # Conversation evidence supports its admission analysis directly; it
        # has no inferred episode between the owner's words and the claim.
        affected_analysis_ids.extend(
            link.target_id for link in links if link.target_type == "analysis"
        )
        affected_memories = []
        if affected_analysis_ids:
            memory_links = (
                db.execute(
                    select(ObjectLink).where(
                        ObjectLink.source_type == "analysis",
                        ObjectLink.source_id.in_(affected_analysis_ids),
                        ObjectLink.target_type == "memory",
                    )
                )
                .scalars()
                .all()
            )
            for memory_link in memory_links:
                memory_link.confidence = 0.0
                memory_link.metadata_json = json.dumps(
                    {"invalidated": True, "reason": reason, "via": evidence_id}
                )
                memory = db.get(Memory, memory_link.target_id)
                if memory and memory.source_type in {"automatic_listener", "owner_statement"}:
                    db.flush()
                    active_support = (
                        db.execute(
                            select(ObjectLink).where(
                                ObjectLink.target_type == "memory",
                                ObjectLink.target_id == memory.id,
                                ObjectLink.relationship == "supports",
                                ObjectLink.confidence > 0,
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if not active_support:
                        mark_unsupported(db, memory, f"all evidence support invalidated: {reason}")
                        affected_memories.append(memory.id)
        db.commit()
        return {
            "status": "ok",
            "affected_links": len(links),
            "memories_needing_review": affected_memories,
        }
    finally:
        db.close()
