import contextlib
import json

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.db import SessionLocal
from app.models.entity import Entity, EntityEdge, EntityEvent, EntityHistory
from app.schemas.entity import (
    EntityCreateRequest,
    EntityEventCreateRequest,
    EntityLinkRequest,
    EntityMergeRequest,
    EntitySearchRequest,
    EntityUpdateByIdRequest,
    EntityUpdateRequest,
)
from app.services.entity_dedup import find_duplicate, merge_entities
from app.services.qdrant_store import (
    delete_entity_embedding,
    index_after_commit,
    upsert_entity_embedding,
)
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

router = APIRouter(prefix="/entity", tags=["entity"])

# Entities are nouns in the world - people/projects/places/orgs, plus
# concept/habit for things with genuine relational structure (recurring
# habits with logged events, topics linked to multiple people). Preferences,
# traits, and other plain attributes belong in `memories`, not here - see
# the "entity_type validation" note in the README's Notable gaps. Mirrors
# CURRENT_MEMORY_TYPES in services/classifier.py and the dashboard's
# ENTITY_TYPE_COLORS (dashboard/src/components/EntityTypeBadge.jsx) - keep
# all three in sync by hand, there's no single source of truth across
# Python/JS.
CURRENT_ENTITY_TYPES = {"human", "project", "organization", "place", "concept", "habit", "object"}

def _entity_to_dict(row: Entity) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "entity_type": row.entity_type,
        "name": row.name,
        "description": row.description,
        "tags": json.loads(row.tags_json),
        "attributes": json.loads(row.attributes_json),
        "agent_notes": row.agent_notes,
        "agent_summary": row.agent_summary,
        "importance_level": row.importance_level,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

def _get_owned_entity(db, entity_id: str, agent_id: str) -> Entity:
    row = db.get(Entity, entity_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Entity not found")
    return row

@router.get("")
def list_entities(agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Entity).where(Entity.agent_id == agent_id).order_by(Entity.updated_at.desc()).limit(100)
        ).scalars().all()
        return [_entity_to_dict(row) for row in rows]
    finally:
        db.close()

@router.post("/create")
def create_entity(payload: EntityCreateRequest, header_agent_id: str = Depends(get_agent_id)):
    if payload.entity_type not in CURRENT_ENTITY_TYPES:
        raise HTTPException(
            422,
            f"Unrecognized entity_type '{payload.entity_type}'. Must be one of {sorted(CURRENT_ENTITY_TYPES)}. "
            f"A plain preference or trait with no relational structure belongs in /memory, not here.",
        )

    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        dup, dedup = find_duplicate(db, agent_id, payload.entity_type, payload.name)
        if dup:
            before = _entity_to_dict(dup)

            old_tags = json.loads(dup.tags_json)
            merged_tags = list(old_tags)
            for tag in payload.tags:
                if tag not in merged_tags:
                    merged_tags.append(tag)
            dup.tags_json = json.dumps(merged_tags)

            merged_attrs = json.loads(dup.attributes_json)
            for key, value in payload.attributes.items():
                merged_attrs.setdefault(key, value)
            dup.attributes_json = json.dumps(merged_attrs)

            dup.description = dup.description or payload.description
            dup.agent_notes = dup.agent_notes or payload.agent_notes
            dup.agent_summary = dup.agent_summary or payload.agent_summary

            db.commit()
            db.refresh(dup)

            db.add(EntityHistory(
                entity_id=dup.id,
                changed_field="entity_deduplicated",
                old_value_json=json.dumps(before),
                new_value_json=json.dumps(_entity_to_dict(dup)),
                change_reason="create request matched an existing entity - merged instead of duplicating",
                triggered_by="system",
            ))
            db.commit()

            return {"status": "ok", "entity": _entity_to_dict(dup), "deduplicated": True}

        entity = Entity(
            agent_id=agent_id,
            entity_type=payload.entity_type,
            name=payload.name,
            description=payload.description,
            tags_json=json.dumps(payload.tags),
            attributes_json=json.dumps(payload.attributes),
            agent_notes=payload.agent_notes,
            agent_summary=payload.agent_summary,
            importance_level=payload.importance_level,
        )
        db.add(entity)
        db.commit()
        db.refresh(entity)

        indexing = index_after_commit(upsert_entity_embedding, entity.id, entity.name, payload={"agent_id": agent_id, "entity_type": entity.entity_type})

        db.add(EntityHistory(
            entity_id=entity.id,
            changed_field="entity_created",
            old_value_json="null",
            new_value_json=json.dumps(_entity_to_dict(entity)),
            change_reason="entity created",
            triggered_by="system",
        ))
        db.commit()

        result = {"status": "ok", "entity": _entity_to_dict(entity)}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        if dedup["status"] != "ok":
            result["dedup"] = dedup
        return result
    finally:
        db.close()

@router.delete("/{entity_id}")
def delete_entity(entity_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_entity(db, entity_id, agent_id)
        from app.models.deletion_receipt import record
        record(db, agent_id, "entity", row.id)
        db.delete(row)
        db.commit()
        # Postgres (the source of truth) already committed the delete -
        # a stale/malformed Qdrant point shouldn't turn a successful
        # delete into a 500.
        with contextlib.suppress(Exception):
            delete_entity_embedding(entity_id)
        return {"status": "ok"}
    finally:
        db.close()

@router.get("/{entity_id}")
def get_entity(entity_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_entity(db, entity_id, agent_id)
        return _entity_to_dict(row)
    finally:
        db.close()

@router.post("/search")
def search_entities(payload: EntitySearchRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        stmt = select(Entity).where(
            Entity.agent_id == agent_id,
            or_(
                Entity.name.ilike(f"%{payload.query}%"),
                Entity.description.ilike(f"%{payload.query}%"),
                Entity.agent_summary.ilike(f"%{payload.query}%"),
                Entity.agent_notes.ilike(f"%{payload.query}%"),
            )
        )
        if payload.entity_type:
            stmt = stmt.where(Entity.entity_type == payload.entity_type)

        rows = db.execute(stmt.order_by(Entity.updated_at.desc()).limit(20)).scalars().all()
        return {"results": [_entity_to_dict(row) for row in rows]}
    finally:
        db.close()

@router.patch("/{entity_id}")
def update_entity(entity_id: str, payload: EntityUpdateRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_entity(db, entity_id, agent_id)

        before = _entity_to_dict(row)
        name_changed = payload.name is not None and payload.name != row.name

        if payload.name is not None:
            row.name = payload.name
        if payload.description is not None:
            row.description = payload.description
        if payload.tags is not None:
            row.tags_json = json.dumps(payload.tags)
        if payload.attributes is not None:
            row.attributes_json = json.dumps(payload.attributes)
        if payload.agent_notes is not None:
            row.agent_notes = payload.agent_notes
        if payload.agent_summary is not None:
            row.agent_summary = payload.agent_summary
        if payload.importance_level is not None:
            row.importance_level = payload.importance_level

        db.commit()
        db.refresh(row)

        indexing = {"status": "ok"}
        if name_changed:
            indexing = index_after_commit(upsert_entity_embedding, row.id, row.name, payload={"agent_id": row.agent_id, "entity_type": row.entity_type})

        after = _entity_to_dict(row)

        db.add(EntityHistory(
            entity_id=row.id,
            changed_field="entity_updated",
            old_value_json=json.dumps(before),
            new_value_json=json.dumps(after),
            change_reason=payload.change_reason,
            triggered_by=payload.triggered_by,
        ))
        db.commit()

        result = {"status": "ok", "entity": after}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        return result
    finally:
        db.close()

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

        db.add(EntityHistory(
            entity_id=keep.id,
            changed_field="entity_merged",
            old_value_json=json.dumps({"merged_entity_id": payload.merge_entity_id}),
            new_value_json=json.dumps(_entity_to_dict(keep)),
            change_reason="manual merge from the entity graph",
            triggered_by="user",
        ))
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

        rows = db.execute(
            select(EntityEdge).where(
                or_(
                    EntityEdge.from_entity_id == entity_id,
                    EntityEdge.to_entity_id == entity_id,
                )
            )
        ).scalars().all()

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

        rows = db.execute(
            select(EntityEvent).where(EntityEvent.entity_id == entity_id).order_by(EntityEvent.created_at.desc())
        ).scalars().all()

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

        rows = db.execute(
            select(EntityHistory).where(EntityHistory.entity_id == entity_id).order_by(EntityHistory.snapshot_at.desc())
        ).scalars().all()

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


@router.post("/update")
def update_entity_by_id(payload: EntityUpdateByIdRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = _get_owned_entity(db, payload.entity_id, agent_id)

        before = _entity_to_dict(row)
        name_changed = payload.name is not None and payload.name != row.name

        if payload.name is not None:
            row.name = payload.name
        if payload.description is not None:
            row.description = payload.description
        if payload.tags is not None:
            row.tags_json = json.dumps(payload.tags)
        if payload.attributes is not None:
            row.attributes_json = json.dumps(payload.attributes)
        if payload.agent_notes is not None:
            row.agent_notes = payload.agent_notes
        if payload.agent_summary is not None:
            row.agent_summary = payload.agent_summary
        if payload.importance_level is not None:
            row.importance_level = payload.importance_level

        db.commit()
        db.refresh(row)

        indexing = {"status": "ok"}
        if name_changed:
            indexing = index_after_commit(upsert_entity_embedding, row.id, row.name, payload={"agent_id": row.agent_id, "entity_type": row.entity_type})

        after = _entity_to_dict(row)

        db.add(EntityHistory(
            entity_id=row.id,
            changed_field="entity_updated",
            old_value_json=json.dumps(before),
            new_value_json=json.dumps(after),
            change_reason=payload.change_reason,
            triggered_by=payload.triggered_by,
        ))
        db.commit()

        result = {"status": "ok", "entity": after}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        return result
    finally:
        db.close()
