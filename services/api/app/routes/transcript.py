import json
from datetime import datetime

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from app.models.session_transcript import SessionTranscript
from app.schemas.transcript import TranscriptCreateRequest
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

router = APIRouter(prefix="/transcripts", tags=["transcripts"])

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def _summary_dict(row: SessionTranscript) -> dict:
    """Metadata only - no transcript text, which can be arbitrarily large."""
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "session_id": row.session_id,
        "session_start": row.session_start.isoformat() if row.session_start else None,
        "session_end": row.session_end.isoformat() if row.session_end else None,
        "word_count": row.word_count,
        "processed_by_soulgate": row.processed_by_soulgate,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }

def _get_owned_transcript(db, transcript_id: str, agent_id: str) -> SessionTranscript:
    row = db.get(SessionTranscript, transcript_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Transcript not found")
    return row

@router.post("")
def create_transcript(payload: TranscriptCreateRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        row = SessionTranscript(
            agent_id=agent_id,
            session_id=payload.session_id,
            transcript=payload.transcript,
            session_start=_parse_dt(payload.session_start),
            session_end=_parse_dt(payload.session_end),
            word_count=payload.word_count if payload.word_count is not None else len(payload.transcript.split()),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        db.add(MemoryAudit(
            action="transcript_write",
            memory_id=None,
            payload_json=json.dumps({
                "agent_id": agent_id,
                "session_id": payload.session_id,
                "word_count": row.word_count,
            }),
        ))
        db.commit()
        return {"status": "ok", "transcript": _summary_dict(row)}
    finally:
        db.close()

@router.get("/{agent_id}")
def list_transcripts(agent_id: str):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(SessionTranscript).where(SessionTranscript.agent_id == agent_id).order_by(SessionTranscript.created_at.desc())
        ).scalars().all()
        return {"results": [_summary_dict(row) for row in rows]}
    finally:
        db.close()

@router.get("/{transcript_id}/full")
def get_full_transcript(transcript_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_transcript(db, transcript_id, agent_id)
        return {**_summary_dict(row), "transcript": row.transcript}
    finally:
        db.close()

@router.post("/{transcript_id}/reprocess")
def reprocess_transcript(transcript_id: str, agent_id: str = Depends(get_agent_id)):
    """Flips processed_by_soulgate back to false so SoulGate's own worker
    picks this transcript up again on its next pass - MemoryGate is just the
    archive, it doesn't invoke SoulGate itself."""
    db = SessionLocal()
    try:
        row = _get_owned_transcript(db, transcript_id, agent_id)
        row.processed_by_soulgate = False
        db.commit()
        db.refresh(row)
        return {"status": "ok", "transcript": _summary_dict(row)}
    finally:
        db.close()

@router.post("/{transcript_id}/mark-processed")
def mark_transcript_processed(transcript_id: str, agent_id: str = Depends(get_agent_id)):
    """SoulGate calls this once it's done extracting memories/observations
    from a transcript. Nothing else in this codebase ever sets
    processed_by_soulgate=true - without this endpoint the flag can never
    leave its false default, and a transcript SoulGate already processed
    would look eligible for reprocessing (duplicate extraction) forever."""
    db = SessionLocal()
    try:
        row = _get_owned_transcript(db, transcript_id, agent_id)
        row.processed_by_soulgate = True
        db.commit()
        db.refresh(row)
        return {"status": "ok", "transcript": _summary_dict(row)}
    finally:
        db.close()
