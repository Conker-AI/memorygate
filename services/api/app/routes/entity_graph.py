"""Entity routes for merging, linking, events and history."""

import json

from app.core.agent import get_agent_id
from app.core.db import SessionLocal
from app.models.entity import EntityEdge, EntityEvent, EntityHistory
from app.routes.entity_common import _entity_to_dict, _get_owned_entity
from app.schemas.entity import EntityEventCreateRequest, EntityLinkRequest, EntityMergeRequest
from app.services.entity_dedup import merge_entities
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

router = APIRouter()


@router.post("/merge")
def merge_entity(payload: EntityMergeRequest, agent_id: str = Depends(get_agent_id)):
    if payload.keep_entity_id == payload.merge_entity_id:
        raise HTTPException(400, "keep_entity_id and merge_entity_id must be different")

    db = SessionLocal()
    try:
        try:
            keep = merge_entities(db, agent_id, payload.keep_entity_id, payload.merge_entity_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

        db.add(
            EntityHistory(
                entity_id=keep.id,
                changed_field="entity_merged",
                old_value_json=json.dumps({"merged_entity_id": payload.merge_entity_id}),
                new_value_json=json.dumps(_entity_to_dict(keep)),
                change_reason="manual merge from the entity graph",
                triggered_by="user",
            )
        )
        db.commit()
        db.refresh(keep)

        return {"status": "ok", "entity": _entity_to_dict(keep)}
    finally:
        db.close()


@router.post("/link")
def link_entities(payload: EntityLinkRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        _get_owned_entity(db, payload.from_entity_id, agent_id)
        _get_owned_entity(db, payload.to_entity_id, agent_id)

        edge = EntityEdge(
            from_entity_id=payload.from_entity_id,
            to_entity_id=payload.to_entity_id,
            relationship_type=payload.relationship_type,
            strength=payload.strength,
            direction=payload.direction,
            notes=payload.notes,
            since_when=payload.since_when,
        )
        db.add(edge)
        db.commit()

        return {
            "status": "ok",
            "edge": {
                "id": edge.id,
                "from_entity_id": edge.from_entity_id,
                "to_entity_id": edge.to_entity_id,
                "relationship_type": edge.relationship_type,
                "strength": edge.strength,
                "direction": edge.direction,
                "notes": edge.notes,
                "since_when": edge.since_when,
            },
        }
    finally:
        db.close()


@router.get("/{entity_id}/edges")
def get_entity_edges(entity_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        _get_owned_entity(db, entity_id, agent_id)

        rows = (
            db.execute(
                select(EntityEdge).where(
                    or_(
                        EntityEdge.from_entity_id == entity_id,
                        EntityEdge.to_entity_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        return {
            "results": [
                {
                    "id": row.id,
                    "from_entity_id": row.from_entity_id,
                    "to_entity_id": row.to_entity_id,
                    "relationship_type": row.relationship_type,
                    "strength": row.strength,
                    "direction": row.direction,
                    "notes": row.notes,
                    "since_when": row.since_when,
                }
                for row in rows
            ]
        }
    finally:
        db.close()


@router.post("/event")
def create_entity_event(payload: EntityEventCreateRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        _get_owned_entity(db, payload.entity_id, agent_id)

        event = EntityEvent(
            entity_id=payload.entity_id,
            event_type=payload.event_type,
            description=payload.description,
            emotional_weight=payload.emotional_weight,
            occurred_at=payload.occurred_at,
        )
        db.add(event)
        db.commit()
        db.refresh(event)

        return {
            "status": "ok",
            "event": {
                "id": event.id,
                "entity_id": event.entity_id,
                "event_type": event.event_type,
                "description": event.description,
                "emotional_weight": event.emotional_weight,
                "occurred_at": event.occurred_at,
            },
        }
    finally:
        db.close()


@router.get("/{entity_id}/events")
def get_entity_events(entity_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        _get_owned_entity(db, entity_id, agent_id)

        rows = (
            db.execute(
                select(EntityEvent)
                .where(EntityEvent.entity_id == entity_id)
                .order_by(EntityEvent.created_at.desc())
            )
            .scalars()
            .all()
        )

        return {
            "results": [
                {
                    "id": row.id,
                    "entity_id": row.entity_id,
                    "event_type": row.event_type,
                    "description": row.description,
                    "emotional_weight": row.emotional_weight,
                    "occurred_at": row.occurred_at,
                }
                for row in rows
            ]
        }
    finally:
        db.close()


@router.get("/{entity_id}/history")
def get_entity_history(entity_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        _get_owned_entity(db, entity_id, agent_id)

        rows = (
            db.execute(
                select(EntityHistory)
                .where(EntityHistory.entity_id == entity_id)
                .order_by(EntityHistory.snapshot_at.desc())
            )
            .scalars()
            .all()
        )

        return {
            "results": [
                {
                    "id": row.id,
                    "entity_id": row.entity_id,
                    "changed_field": row.changed_field,
                    "old_value": json.loads(row.old_value_json),
                    "new_value": json.loads(row.new_value_json),
                    "change_reason": row.change_reason,
                    "triggered_by": row.triggered_by,
                    "snapshot_at": row.snapshot_at.isoformat() if row.snapshot_at else None,
                }
                for row in rows
            ]
        }
    finally:
        db.close()
