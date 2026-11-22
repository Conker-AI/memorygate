"""Row helpers shared by the entity route modules."""

import json

from app.models.entity import Entity
from fastapi import HTTPException


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
