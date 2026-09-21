import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from app.core.agent import get_agent_id, resolve_agent_id
from app.core.auth import require_read_key
from app.core.db import SessionLocal
from app.models.memory import Memory
from app.schemas.skill import SkillPatchRequest, SkillWriteRequest

router = APIRouter(prefix="/skills", tags=["skills"])
context_router = APIRouter(prefix="/context", tags=["context"])

MAX_SKILL_BYTES = 8192


def _skill_tags(linked_tools: list[str], version: str, active: bool) -> str:
    return json.dumps({
        "kind": "skill",
        "linked_tools": linked_tools,
        "version": version,
        "active": active,
    }, ensure_ascii=True, sort_keys=True)


def _parse_tags(row: Memory) -> dict:
    try:
        value = json.loads(row.tags_json or "{}")
    except json.JSONDecodeError:
        value = {}
    if isinstance(value, list):
        return {"kind": "skill", "linked_tools": value, "version": "1", "active": row.status == "active"}
    return value if isinstance(value, dict) else {}


def _row_to_skill(row: Memory, include_body: bool = True) -> dict:
    tags = _parse_tags(row)
    body = row.text
    if len(body.encode("utf-8")) > MAX_SKILL_BYTES:
        body = body.encode("utf-8")[:MAX_SKILL_BYTES].decode("utf-8", errors="ignore")
    result = {
        "id": row.id,
        "agent_id": row.agent_id,
        "title": row.summary,
        "linked_tools": tags.get("linked_tools", []),
        "version": str(tags.get("version", "1")),
        "active": bool(tags.get("active", row.status == "active")),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_body:
        result["body"] = body
    return result


def _require_skill(row: Memory | None, agent_id: str) -> Memory:
    if not row or row.agent_id != agent_id or row.memory_type != "skill":
        raise HTTPException(404, "Skill not found")
    return row


@router.get("")
def list_skills(agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Memory)
            .where(Memory.agent_id == agent_id, Memory.memory_type == "skill")
            .order_by(Memory.updated_at.desc())
        ).scalars().all()
        return {"results": [_row_to_skill(row) for row in rows]}
    finally:
        db.close()


@router.post("")
def create_skill(payload: SkillWriteRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        row = Memory(
            agent_id=agent_id,
            text=payload.body,
            summary=payload.title,
            memory_type="skill",
            source_type="skill_editor",
            confidence="high",
            tags_json=_skill_tags(payload.linked_tools, payload.version, payload.active),
            status="active" if payload.active else "inactive",
            valid_from=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"status": "ok", "skill": _row_to_skill(row)}
    finally:
        db.close()


@router.get("/{skill_id}")
def get_skill(skill_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        return _row_to_skill(_require_skill(db.get(Memory, skill_id), agent_id))
    finally:
        db.close()


@router.patch("/{skill_id}")
def update_skill(skill_id: str, payload: SkillPatchRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _require_skill(db.get(Memory, skill_id), agent_id)
        tags = _parse_tags(row)
        if payload.title is not None:
            row.summary = payload.title
        if payload.body is not None:
            row.text = payload.body
        if payload.linked_tools is not None:
            tags["linked_tools"] = payload.linked_tools
        if payload.version is not None:
            tags["version"] = payload.version
        if payload.active is not None:
            tags["active"] = payload.active
            row.status = "active" if payload.active else "inactive"
        row.tags_json = _skill_tags(tags.get("linked_tools", []), str(tags.get("version", "1")), bool(tags.get("active", row.status == "active")))
        db.commit()
        db.refresh(row)
        return {"status": "ok", "skill": _row_to_skill(row)}
    finally:
        db.close()


@router.delete("/{skill_id}")
def delete_skill(skill_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _require_skill(db.get(Memory, skill_id), agent_id)
        from app.models.deletion_receipt import record
        record(db, agent_id, "memory", row.id)
        db.delete(row)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


@context_router.get("/skills", dependencies=[Depends(require_read_key)])
def context_skills(tool: str = Query(min_length=1, max_length=200), agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Memory)
            .where(Memory.agent_id == agent_id, Memory.memory_type == "skill", Memory.status == "active")
            .order_by(Memory.updated_at.desc())
            .limit(50)
        ).scalars().all()
        matches = []
        total = 0
        for row in rows:
            tags = _parse_tags(row)
            linked = [str(item) for item in tags.get("linked_tools", [])]
            if tool not in linked:
                continue
            item = _row_to_skill(row)
            size = len((item["title"] + item["body"]).encode("utf-8"))
            if total + size > MAX_SKILL_BYTES:
                break
            total += size
            matches.append(item)
        return {"results": matches, "tool": tool, "bytes": total}
    finally:
        db.close()
