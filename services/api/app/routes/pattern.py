import json
from datetime import UTC, datetime

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.db import SessionLocal
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.schemas.pattern import (
    PatternConfirmRequest,
    PatternContradictRequest,
    PatternCreateRequest,
    PatternPromoteRequest,
    PatternSearchRequest,
    PatternUpdateRequest,
)
from app.services.pattern_promotion import (
    DEPRECATE_AT_CONTRADICTIONS,
    clamp_confidence,
    maybe_promote,
)
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

router = APIRouter(prefix="/pattern", tags=["pattern"])

def _pattern_to_dict(row: Pattern) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "pattern_name": row.pattern_name,
        "description": row.description,
        "observation_ids": json.loads(row.observation_ids_json),
        "instance_count": row.instance_count,
        "confirmation_count": row.confirmation_count,
        "contradiction_count": row.contradiction_count,
        "confidence": row.confidence,
        "interpretation": row.interpretation,
        "recommended_action": row.recommended_action,
        "applies_to_entity_ids": json.loads(row.applies_to_entity_ids_json),
        "context_conditions": json.loads(row.context_conditions_json),
        "status": row.status,
        "promoted_at": row.promoted_at.isoformat() if row.promoted_at else None,
        "last_confirmed_at": row.last_confirmed_at.isoformat() if row.last_confirmed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

def _get_owned_pattern(db, pattern_id: str, agent_id: str) -> Pattern:
    row = db.get(Pattern, pattern_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Pattern not found")
    return row

@router.post("/create")
def create_pattern(payload: PatternCreateRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        row = Pattern(
            agent_id=agent_id,
            pattern_name=payload.pattern_name,
            description=payload.description,
            observation_ids_json=json.dumps(payload.observation_ids),
            instance_count=payload.instance_count,
            confirmation_count=payload.confirmation_count,
            contradiction_count=payload.contradiction_count,
            confidence=clamp_confidence(payload.confidence),
            interpretation=payload.interpretation,
            recommended_action=payload.recommended_action,
            applies_to_entity_ids_json=json.dumps(payload.applies_to_entity_ids),
            context_conditions_json=json.dumps(payload.context_conditions),
            status=payload.status,
            promoted_at=datetime.now(UTC) if payload.status == "active" else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"status": "ok", "pattern": _pattern_to_dict(row)}
    finally:
        db.close()

@router.post("/search")
def search_patterns(payload: PatternSearchRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Pattern).where(Pattern.agent_id == agent_id).order_by(Pattern.updated_at.desc())
        ).scalars().all()
        results = []

        for row in rows:
            item = _pattern_to_dict(row)

            if payload.status and item["status"] != payload.status:
                continue
            if payload.entity_id and payload.entity_id not in item["applies_to_entity_ids"]:
                continue

            if payload.query:
                hay = " ".join([
                    item["pattern_name"],
                    item["description"],
                    item["interpretation"],
                    item["recommended_action"],
                    item["status"],
                ]).lower()
                if payload.query.lower() not in hay:
                    continue

            results.append(item)

        return {"results": results[:50]}
    finally:
        db.close()

@router.get("/active/{agent_id}")
def list_active_patterns(agent_id: str):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Pattern).where(Pattern.agent_id == agent_id, Pattern.status == "active").order_by(Pattern.updated_at.desc())
        ).scalars().all()
        return {"results": [_pattern_to_dict(row) for row in rows]}
    finally:
        db.close()

@router.get("/candidates/{agent_id}")
def list_candidate_patterns(agent_id: str):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Pattern).where(Pattern.agent_id == agent_id, Pattern.status == "candidate").order_by(Pattern.updated_at.desc())
        ).scalars().all()
        return {"results": [_pattern_to_dict(row) for row in rows]}
    finally:
        db.close()

@router.get("/{pattern_id}")
def get_pattern(pattern_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_pattern(db, pattern_id, agent_id)
        return _pattern_to_dict(row)
    finally:
        db.close()

@router.post("/update/{pattern_id}")
def update_pattern(pattern_id: str, payload: PatternUpdateRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_pattern(db, pattern_id, agent_id)

        if payload.description is not None:
            row.description = payload.description
        if payload.observation_ids is not None:
            row.observation_ids_json = json.dumps(payload.observation_ids)
        if payload.instance_count is not None:
            row.instance_count = payload.instance_count
        if payload.confirmation_count is not None:
            row.confirmation_count = payload.confirmation_count
        if payload.contradiction_count is not None:
            row.contradiction_count = payload.contradiction_count
        if payload.confidence is not None:
            row.confidence = clamp_confidence(payload.confidence)
        if payload.interpretation is not None:
            row.interpretation = payload.interpretation
        if payload.recommended_action is not None:
            row.recommended_action = payload.recommended_action
        if payload.applies_to_entity_ids is not None:
            row.applies_to_entity_ids_json = json.dumps(payload.applies_to_entity_ids)
        if payload.context_conditions is not None:
            row.context_conditions_json = json.dumps(payload.context_conditions)
        if payload.status is not None:
            row.status = payload.status
            if payload.status == "active" and row.promoted_at is None:
                row.promoted_at = datetime.now(UTC)

        db.commit()
        db.refresh(row)
        return {"status": "ok", "pattern": _pattern_to_dict(row)}
    finally:
        db.close()

@router.post("/{pattern_id}/confirm")
def confirm_pattern(pattern_id: str, payload: PatternConfirmRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_pattern(db, pattern_id, agent_id)
        row.confirmation_count += 1
        row.last_confirmed_at = datetime.now(UTC)
        maybe_promote(row)
        db.commit()
        db.refresh(row)
        return {"status": "ok", "pattern": _pattern_to_dict(row)}
    finally:
        db.close()

@router.post("/{pattern_id}/contradict")
def contradict_pattern(pattern_id: str, payload: PatternContradictRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_pattern(db, pattern_id, agent_id)
        row.contradiction_count += 1
        if row.status == "active" and row.contradiction_count >= DEPRECATE_AT_CONTRADICTIONS:
            row.status = "deprecated"
        db.commit()
        db.refresh(row)
        return {"status": "ok", "pattern": _pattern_to_dict(row)}
    finally:
        db.close()

@router.post("/promote")
def promote_pattern(payload: PatternPromoteRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        obs_rows = db.execute(
            select(Observation).where(Observation.agent_id == agent_id).order_by(Observation.observed_at.desc())
        ).scalars().all()
        matched = []

        for row in obs_rows:
            entity_ids = json.loads(row.entity_ids_json)
            if payload.entity_id and payload.entity_id not in entity_ids:
                continue

            if payload.query:
                hay = " ".join([
                    row.description,
                    row.raw_context,
                    row.hypothesis,
                    row.signal_type,
                    row.status,
                ]).lower()
                if payload.query.lower() not in hay:
                    continue

            matched.append(row)

        if len(matched) < payload.min_observations:
            return {
                "status": "not_enough_evidence",
                "matched_count": len(matched),
                "required": payload.min_observations,
            }

        observation_ids = [row.id for row in matched]
        applies_to_entity_ids = []
        seen = set()
        for row in matched:
            for eid in json.loads(row.entity_ids_json):
                if eid not in seen:
                    seen.add(eid)
                    applies_to_entity_ids.append(eid)

        confidence = clamp_confidence(payload.confidence)
        pattern = Pattern(
            agent_id=agent_id,
            pattern_name=payload.pattern_name,
            description=f"Promoted from {len(matched)} observations.",
            observation_ids_json=json.dumps(observation_ids),
            instance_count=len(matched),
            confirmation_count=len(matched),
            contradiction_count=0,
            confidence=confidence,
            interpretation=payload.interpretation,
            recommended_action=payload.recommended_action,
            applies_to_entity_ids_json=json.dumps(applies_to_entity_ids),
            context_conditions_json=json.dumps({}),
            status="candidate" if confidence < 0.85 else "active",
            promoted_at=datetime.now(UTC) if confidence >= 0.85 else None,
            last_confirmed_at=datetime.now(UTC),
        )
        db.add(pattern)
        db.commit()
        db.refresh(pattern)

        return {"status": "ok", "pattern": _pattern_to_dict(pattern)}
    finally:
        db.close()
