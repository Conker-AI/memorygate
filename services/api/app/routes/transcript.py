"""Legacy transcript archive: read and delete only.

Transcripts live only in Pi (ADR-0002); MemoryGate gets derived evidence. The archive
is left from the SoulGate era, so it takes no new transcripts. The rows already there
can be listed, read and deleted. A delete removes the lineage links naming it and leaves
content-free deletion receipts, so restoring an older backup does not bring the
transcript back (deletion_recovery.replay).
"""

import json

from app.core.agent import get_agent_id
from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from app.models.deletion_receipt import record
from app.models.object_link import ObjectLink
from app.models.session_transcript import SessionTranscript
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

router = APIRouter(prefix="/transcripts", tags=["transcripts"])

def _summary_dict(row: SessionTranscript) -> dict:
    """Metadata only - no transcript text, which can be arbitrarily large."""
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "session_id": row.session_id,
        "session_start": row.session_start.isoformat() if row.session_start else None,
        "session_end": row.session_end.isoformat() if row.session_end else None,
        "word_count": row.word_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }

def _get_owned_transcript(db, transcript_id: str, agent_id: str) -> SessionTranscript:
    row = db.get(SessionTranscript, transcript_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Transcript not found")
    return row

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

@router.delete("/{transcript_id}")
def delete_transcript(transcript_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_transcript(db, transcript_id, agent_id)
        record(db, agent_id, "transcript", row.id)
        links = db.execute(select(ObjectLink).where(or_(
            (ObjectLink.source_type == "transcript") & (ObjectLink.source_id == row.id),
            (ObjectLink.target_type == "transcript") & (ObjectLink.target_id == row.id),
        ))).scalars().all()
        for link in links:
            record(db, "", "link", link.id)
            db.delete(link)
        db.add(MemoryAudit(
            action="transcript_delete",
            memory_id=None,
            payload_json=json.dumps({"agent_id": agent_id, "transcript_id": row.id}),
        ))
        db.delete(row)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()
