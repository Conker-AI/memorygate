import contextlib
import json
from datetime import UTC, datetime

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.db import SessionLocal
from app.models.observation import Observation
from app.schemas.observation import (
    ObservationArchiveRequest,
    ObservationConfirmRequest,
    ObservationContradictRequest,
    ObservationCreateRequest,
    ObservationSearchRequest,
    ObservationSessionContextRequest,
    ObservationUpdateRequest,
)
from app.services.agent_config_service import get_or_create_config
from app.services.observation_lifecycle import apply_session_context, enforce_budget, find_duplicate
from app.services.pattern_promotion import promote_from_observations
from app.services.qdrant_store import (
    delete_observation_embedding,
    index_after_commit,
    upsert_observation_embedding,
)
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select

router = APIRouter(prefix="/observation", tags=["observation"])

def _obs_to_dict(row: Observation) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "session_id": row.session_id,
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        "signal_type": row.signal_type,
        "description": row.description,
        "raw_context": row.raw_context,
        "hypothesis": row.hypothesis,
        "hypothesis_confidence": row.hypothesis_confidence,
        "status": row.status,
        "confirmed_by": row.confirmed_by,
        "confirmation_count": row.confirmation_count,
        "exposure_count": row.exposure_count,
        "max_exposures": row.max_exposures,
        "trigger_context": row.trigger_context,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        "archive_reason": row.archive_reason,
        "raise_condition": row.raise_condition,
        "needs_clarification": row.needs_clarification,
        "entity_ids": json.loads(row.entity_ids_json),
        "related_observation_ids": json.loads(row.related_observation_ids_json),
    }

def _get_owned_observation(db, observation_id: str, agent_id: str) -> Observation:
    row = db.get(Observation, observation_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Observation not found")
    return row

@router.post("/create")
def create_observation(payload: ObservationCreateRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        dup, dedup = find_duplicate(agent_id, payload.signal_type, payload.description)
        if dup:
            row = db.get(Observation, dup["id"])
            if row and row.agent_id == agent_id:
                row.confirmation_count += 1
                db.commit()
                db.refresh(row)
                return {"status": "ok", "observation": _obs_to_dict(row), "deduplicated": True, "similarity": dup["score"]}

        config = get_or_create_config(db, agent_id)
        enforce_budget(db, agent_id, config.max_observations)

        row = Observation(
            agent_id=agent_id,
            session_id=payload.session_id,
            signal_type=payload.signal_type,
            description=payload.description,
            raw_context=payload.raw_context,
            hypothesis=payload.hypothesis,
            hypothesis_confidence=payload.hypothesis_confidence,
            status=payload.status,
            confirmed_by=payload.confirmed_by,
            trigger_context=payload.trigger_context,
            max_exposures=payload.max_exposures,
            raise_condition=payload.raise_condition,
            needs_clarification=payload.needs_clarification,
            entity_ids_json=json.dumps(payload.entity_ids),
            related_observation_ids_json=json.dumps(payload.related_observation_ids),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        indexing = index_after_commit(
            upsert_observation_embedding,
            row.id,
            row.description,
            payload={"agent_id": agent_id, "signal_type": row.signal_type, "status": row.status},
        )

        promote_from_observations(db, agent_id, signal_type=row.signal_type)

        result = {"status": "ok", "observation": _obs_to_dict(row)}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        if dedup["status"] != "ok":
            result["dedup"] = dedup
        return result
    finally:
        db.close()

@router.post("/search")
def search_observations(payload: ObservationSearchRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        stmt = select(Observation).where(Observation.agent_id == agent_id)

        filters = []
        if payload.signal_type:
            filters.append(Observation.signal_type == payload.signal_type)
        if payload.status:
            filters.append(Observation.status == payload.status)
        if payload.needs_clarification is not None:
            filters.append(Observation.needs_clarification == payload.needs_clarification)
        if filters:
            stmt = stmt.where(and_(*filters))

        rows = db.execute(stmt.order_by(Observation.observed_at.desc())).scalars().all()

        results = []
        for row in rows:
            item = _obs_to_dict(row)

            if payload.entity_id and payload.entity_id not in item["entity_ids"]:
                continue

            if payload.query:
                hay = " ".join([
                    item["description"],
                    item["raw_context"],
                    item["hypothesis"],
                    item["status"],
                    item["signal_type"],
                ]).lower()
                if payload.query.lower() not in hay:
                    continue

            results.append(item)

        return {"results": results[:50]}
    finally:
        db.close()

@router.get("/active")
def list_active_observations(agent_id: str = Depends(get_agent_id), needs_clarification: bool | None = None):
    db = SessionLocal()
    try:
        stmt = select(Observation).where(Observation.agent_id == agent_id, Observation.status != "archived")
        if needs_clarification is not None:
            stmt = stmt.where(Observation.needs_clarification == needs_clarification)
        rows = db.execute(stmt.order_by(Observation.observed_at.desc())).scalars().all()
        return {"results": [_obs_to_dict(row) for row in rows]}
    finally:
        db.close()

@router.post("/session-context")
def observation_session_context(payload: ObservationSessionContextRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        result = apply_session_context(db, agent_id, payload.session_context)
        return {"status": "ok", **result}
    finally:
        db.close()

@router.delete("/{observation_id}")
def delete_observation(observation_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_observation(db, observation_id, agent_id)
        from app.models.deletion_receipt import record
        record(db, agent_id, "observation", row.id)
        db.delete(row)
        db.commit()
        # Postgres (the source of truth) already committed the delete -
        # a stale/malformed Qdrant point shouldn't turn a successful
        # delete into a 500.
        with contextlib.suppress(Exception):
            delete_observation_embedding(observation_id)
        return {"status": "ok"}
    finally:
        db.close()

@router.get("/{observation_id}")
def get_observation(observation_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_observation(db, observation_id, agent_id)
        return _obs_to_dict(row)
    finally:
        db.close()

@router.post("/update/{observation_id}")
def update_observation(observation_id: str, payload: ObservationUpdateRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_observation(db, observation_id, agent_id)

        if payload.status is not None:
            row.status = payload.status
        if payload.hypothesis is not None:
            row.hypothesis = payload.hypothesis
        if payload.hypothesis_confidence is not None:
            row.hypothesis_confidence = payload.hypothesis_confidence
        if payload.confirmed_by is not None:
            row.confirmed_by = payload.confirmed_by
        if payload.trigger_context is not None:
            row.trigger_context = payload.trigger_context
        if payload.raise_condition is not None:
            row.raise_condition = payload.raise_condition
        if payload.needs_clarification is not None:
            row.needs_clarification = payload.needs_clarification
        if payload.related_observation_ids is not None:
            row.related_observation_ids_json = json.dumps(payload.related_observation_ids)

        db.commit()
        db.refresh(row)
        return {"status": "ok", "observation": _obs_to_dict(row)}
    finally:
        db.close()

@router.post("/{observation_id}/confirm")
def confirm_observation(observation_id: str, payload: ObservationConfirmRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_observation(db, observation_id, agent_id)
        row.status = "confirmed"
        row.confirmation_count += 1
        if payload.confirmed_by:
            row.confirmed_by = payload.confirmed_by
        db.commit()
        db.refresh(row)

        promote_from_observations(db, agent_id, signal_type=row.signal_type)

        return {"status": "ok", "observation": _obs_to_dict(row)}
    finally:
        db.close()

@router.post("/{observation_id}/contradict")
def contradict_observation(observation_id: str, payload: ObservationContradictRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_observation(db, observation_id, agent_id)
        row.status = "contradicted"
        db.commit()
        db.refresh(row)
        return {"status": "ok", "observation": _obs_to_dict(row)}
    finally:
        db.close()

@router.post("/{observation_id}/archive")
def archive_observation(observation_id: str, payload: ObservationArchiveRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_observation(db, observation_id, agent_id)
        row.status = "archived"
        row.archived_at = datetime.now(UTC)
        row.archive_reason = payload.reason
        db.commit()
        db.refresh(row)
        return {"status": "ok", "observation": _obs_to_dict(row)}
    finally:
        db.close()
