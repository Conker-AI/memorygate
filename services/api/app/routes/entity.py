import json

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.db import SessionLocal
from app.models.entity import Entity, EntityHistory
from app.routes.entity_common import _entity_to_dict, _get_owned_entity
from app.routes.entity_graph import router as _entity_graph_router
from app.schemas.entity import (
    EntityCreateRequest,
    EntitySearchRequest,
    EntityUpdateByIdRequest,
    EntityUpdateRequest,
)
from app.services.entity_dedup import find_duplicate
from app.services.entity_forgetting import erase_dependents
from app.services.lexical_retrieval import escape_like
from app.services.vector_outbox import enqueue_delete, enqueue_upsert, reconcile_one
from fastapi import APIRouter, Depends, HTTPException, Query
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


@router.get("")
def list_entities(
    agent_id: str = Depends(get_agent_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Most recently changed first; `offset` pages to older entities."""
    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(Entity)
                .where(Entity.agent_id == agent_id)
                .order_by(Entity.updated_at.desc(), Entity.id.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
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

            db.add(
                EntityHistory(
                    entity_id=dup.id,
                    changed_field="entity_deduplicated",
                    old_value_json=json.dumps(before),
                    new_value_json=json.dumps(_entity_to_dict(dup)),
                    change_reason="create request matched an existing entity - merged instead of duplicating",
                    triggered_by="system",
                )
            )
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
        db.flush()
        enqueue_upsert(
            db,
            "entity",
            entity.id,
            entity.name,
            agent_id=agent_id,
            payload={"agent_id": agent_id, "entity_type": entity.entity_type},
        )

        db.add(
            EntityHistory(
                entity_id=entity.id,
                changed_field="entity_created",
                old_value_json="null",
                new_value_json=json.dumps(_entity_to_dict(entity)),
                change_reason="entity created",
                triggered_by="system",
            )
        )
        db.commit()
        db.refresh(entity)
        indexing = reconcile_one("entity", entity.id, sessions=SessionLocal)

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
        enqueue_delete(db, "entity", row.id, agent_id=agent_id)
        erase_dependents(db, row.id)
        db.delete(row)
        db.commit()
        indexing = reconcile_one("entity", entity_id, sessions=SessionLocal)
        result: dict[str, object] = {"status": "ok"}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        return result
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
        pattern = f"%{escape_like(payload.query)}%"
        stmt = select(Entity).where(
            Entity.agent_id == agent_id,
            or_(
                Entity.name.ilike(pattern, escape="/"),
                Entity.description.ilike(pattern, escape="/"),
                Entity.agent_summary.ilike(pattern, escape="/"),
                Entity.agent_notes.ilike(pattern, escape="/"),
            ),
        )
        if payload.entity_type:
            stmt = stmt.where(Entity.entity_type == payload.entity_type)

        rows = db.execute(stmt.order_by(Entity.updated_at.desc()).limit(20)).scalars().all()
        return {"results": [_entity_to_dict(row) for row in rows]}
    finally:
        db.close()


@router.patch("/{entity_id}")
def update_entity(
    entity_id: str, payload: EntityUpdateRequest, agent_id: str = Depends(get_agent_id)
):
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

        db.flush()
        indexing = {"status": "ok"}
        if name_changed:
            enqueue_upsert(
                db,
                "entity",
                row.id,
                row.name,
                agent_id=row.agent_id,
                payload={"agent_id": row.agent_id, "entity_type": row.entity_type},
            )

        after = _entity_to_dict(row)

        db.add(
            EntityHistory(
                entity_id=row.id,
                changed_field="entity_updated",
                old_value_json=json.dumps(before),
                new_value_json=json.dumps(after),
                change_reason=payload.change_reason,
                triggered_by=payload.triggered_by,
            )
        )
        db.commit()
        db.refresh(row)
        if name_changed:
            indexing = reconcile_one("entity", row.id, sessions=SessionLocal)

        result = {"status": "ok", "entity": after}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        return result
    finally:
        db.close()


router.include_router(_entity_graph_router)


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

        db.flush()
        indexing = {"status": "ok"}
        if name_changed:
            enqueue_upsert(
                db,
                "entity",
                row.id,
                row.name,
                agent_id=row.agent_id,
                payload={"agent_id": row.agent_id, "entity_type": row.entity_type},
            )

        after = _entity_to_dict(row)

        db.add(
            EntityHistory(
                entity_id=row.id,
                changed_field="entity_updated",
                old_value_json=json.dumps(before),
                new_value_json=json.dumps(after),
                change_reason=payload.change_reason,
                triggered_by=payload.triggered_by,
            )
        )
        db.commit()
        db.refresh(row)
        if name_changed:
            indexing = reconcile_one("entity", row.id, sessions=SessionLocal)

        result = {"status": "ok", "entity": after}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        return result
    finally:
        db.close()
