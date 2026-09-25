import json
from datetime import datetime

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.db import SessionLocal
from app.models.analysis_object import AnalysisObject
from app.models.entity import Entity
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.models.session_transcript import SessionTranscript
from app.schemas.lineage import EpisodeCreateRequest, EpisodeUpdateRequest, ObjectLinkCreateRequest
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

router = APIRouter(prefix="/lineage", tags=["lineage"])

OBJECT_MODELS = {
    "evidence": EvidenceObject,
    "episode": EpisodeObject,
    "analysis": AnalysisObject,
    "memory": Memory,
    "entity": Entity,
    "observation": Observation,
    "pattern": Pattern,
    "transcript": SessionTranscript,
}


def _parse_dt(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _episode_to_dict(row: EpisodeObject) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "title": row.title,
        "summary": row.summary,
        "episode_type": row.episode_type,
        "status": row.status,
        "confidence": row.confidence,
        "tags": json.loads(row.tags_json or "[]"),
        "occurred_start": row.occurred_start.isoformat() if row.occurred_start else None,
        "occurred_end": row.occurred_end.isoformat() if row.occurred_end else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _link_to_dict(row: ObjectLink) -> dict:
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "relationship": row.relationship,
        "confidence": row.confidence,
        "metadata": json.loads(row.metadata_json or "{}"),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _require_object(db, object_type: str, object_id: str):
    model = OBJECT_MODELS.get(object_type)
    if model is None:
        raise HTTPException(422, f"Unknown object type '{object_type}'")
    row = db.get(model, object_id)
    if row is None:
        raise HTTPException(404, f"{object_type} object '{object_id}' not found")
    return row


def _create_link(db, payload: ObjectLinkCreateRequest) -> ObjectLink:
    _require_object(db, payload.source_type, payload.source_id)
    _require_object(db, payload.target_type, payload.target_id)
    existing = db.execute(select(ObjectLink).where(
        ObjectLink.source_type == payload.source_type,
        ObjectLink.source_id == payload.source_id,
        ObjectLink.target_type == payload.target_type,
        ObjectLink.target_id == payload.target_id,
        ObjectLink.relationship == payload.relationship,
    )).scalar_one_or_none()
    if existing:
        return existing
    row = ObjectLink(
        source_type=payload.source_type,
        source_id=payload.source_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        relationship=payload.relationship,
        confidence=payload.confidence,
        metadata_json=json.dumps(payload.metadata),
        created_by=payload.created_by,
    )
    db.add(row)
    db.flush()
    return row


@router.get("/episodes")
def list_episodes(agent_id: str = Depends(get_agent_id), limit: int = 100):
    db = SessionLocal()
    try:
        rows = db.execute(select(EpisodeObject).where(EpisodeObject.agent_id == agent_id).order_by(EpisodeObject.occurred_start.desc(), EpisodeObject.created_at.desc()).limit(max(1, min(limit, 300)))).scalars().all()
        return {"results": [_episode_to_dict(row) for row in rows]}
    finally:
        db.close()


@router.post("/episodes")
def create_episode(payload: EpisodeCreateRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        row = EpisodeObject(
            agent_id=agent_id,
            title=payload.title.strip(),
            summary=payload.summary.strip(),
            episode_type=payload.episode_type.strip().lower(),
            status=payload.status.strip().lower(),
            confidence=payload.confidence,
            tags_json=json.dumps(payload.tags),
            occurred_start=_parse_dt(payload.occurred_start),
            occurred_end=_parse_dt(payload.occurred_end),
        )
        db.add(row)
        db.flush()
        for evidence_id in dict.fromkeys(payload.evidence_ids):
            _create_link(db, ObjectLinkCreateRequest(
                source_type="evidence", source_id=evidence_id,
                target_type="episode", target_id=row.id,
                relationship="grouped_into", created_by="user",
            ))
        db.commit()
        db.refresh(row)
        return {"status": "ok", "episode": _episode_to_dict(row)}
    finally:
        db.close()


@router.patch("/episodes/{episode_id}")
def update_episode(episode_id: str, payload: EpisodeUpdateRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = db.get(EpisodeObject, episode_id)
        if not row or row.agent_id != agent_id:
            raise HTTPException(404, "Episode not found")
        for field in ("title", "summary", "episode_type", "status", "confidence"):
            value = getattr(payload, field)
            if value is not None:
                setattr(row, field, value)
        if payload.tags is not None:
            row.tags_json = json.dumps(payload.tags)
        if payload.occurred_start is not None:
            row.occurred_start = _parse_dt(payload.occurred_start)
        if payload.occurred_end is not None:
            row.occurred_end = _parse_dt(payload.occurred_end)
        db.commit()
        db.refresh(row)
        return {"status": "ok", "episode": _episode_to_dict(row)}
    finally:
        db.close()


@router.delete("/episodes/{episode_id}")
def delete_episode(episode_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = db.get(EpisodeObject, episode_id)
        if not row or row.agent_id != agent_id:
            raise HTTPException(404, "Episode not found")
        from app.models.deletion_receipt import record
        record(db, agent_id, "episode", row.id)
        links = db.execute(select(ObjectLink).where(or_(
            (ObjectLink.source_type == "episode") & (ObjectLink.source_id == episode_id),
            (ObjectLink.target_type == "episode") & (ObjectLink.target_id == episode_id),
        ))).scalars().all()
        for link in links:
            record(db, "", "link", link.id)
            db.delete(link)
        db.delete(row)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


@router.post("/links")
def create_link(payload: ObjectLinkCreateRequest):
    db = SessionLocal()
    try:
        row = _create_link(db, payload)
        db.commit()
        db.refresh(row)
        return {"status": "ok", "link": _link_to_dict(row)}
    finally:
        db.close()


@router.delete("/links/{link_id}")
def delete_link(link_id: str):
    db = SessionLocal()
    try:
        row = db.get(ObjectLink, link_id)
        if not row:
            raise HTTPException(404, "Lineage link not found")
        from app.models.deletion_receipt import record
        record(db, "", "link", row.id)
        db.delete(row)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


@router.get("/{object_type}/{object_id}")
def get_object_lineage(object_type: str, object_id: str):
    db = SessionLocal()
    try:
        _require_object(db, object_type, object_id)
        rows = db.execute(select(ObjectLink).where(or_(
            (ObjectLink.source_type == object_type) & (ObjectLink.source_id == object_id),
            (ObjectLink.target_type == object_type) & (ObjectLink.target_id == object_id),
        )).order_by(ObjectLink.created_at.asc())).scalars().all()
        return {
            "object_type": object_type,
            "object_id": object_id,
            "incoming": [_link_to_dict(row) for row in rows if row.target_type == object_type and row.target_id == object_id],
            "outgoing": [_link_to_dict(row) for row in rows if row.source_type == object_type and row.source_id == object_id],
        }
    finally:
        db.close()
