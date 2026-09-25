"""Dedup check for entity creation - exact name match first, then embedding
similarity, scoped to (agent_id, entity_type) so e.g. a person and a project
that happen to share a name never collide."""
import contextlib
import json

from app.models.entity import Entity
from app.services.qdrant_store import (
    INDEX_UNREACHABLE,
    delete_entity_embedding,
    find_similar_entities,
    semantic_status,
)
from sqlalchemy import func, select, text

DEDUP_SIMILARITY_THRESHOLD = 0.9


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


def find_duplicate(db, agent_id: str, entity_type: str, name: str) -> tuple[Entity | None, dict]:
    """Return the duplicate, if any, and the status of the similarity tier.

    Exact-name matching never needs a vector. When the similarity tier cannot
    run, "no duplicate" means "none found by exact name" - the caller reports
    that rather than presenting a partial check as a complete one.
    """
    normalized = _normalize(name)
    exact = db.execute(
        select(Entity).where(
            Entity.agent_id == agent_id,
            Entity.entity_type == entity_type,
            func.lower(func.trim(Entity.name)) == normalized,
        )
    ).scalars().first()
    if exact:
        return exact, {"status": "ok", "component": None, "reason": None}

    status = semantic_status()
    if status["status"] != "ok":
        return None, status
    try:
        hits = find_similar_entities(name, agent_id=agent_id, entity_type=entity_type, limit=1)
    except Exception:
        return None, {"status": "degraded", "component": "vector_index", "reason": INDEX_UNREACHABLE}
    if hits and hits[0]["score"] >= DEDUP_SIMILARITY_THRESHOLD:
        return db.get(Entity, hits[0]["id"]), status

    return None, status


def merge_entities(db, agent_id: str, keep_id: str, merge_id: str) -> Entity:
    """Manual merge for the graph's 'select two nodes -> merge' action -
    same repointing logic as the startup dedup migration
    (core/migrations.py:_merge_duplicate_entities), just against a live ORM
    session instead of a bare connection, and for a single caller-chosen pair
    instead of every duplicate group at once."""
    if keep_id == merge_id:
        raise ValueError("cannot merge an entity into itself")

    keep = db.get(Entity, keep_id)
    merge = db.get(Entity, merge_id)
    if not keep or keep.agent_id != agent_id or not merge or merge.agent_id != agent_id:
        raise ValueError("entity not found")

    merged_tags = list(json.loads(keep.tags_json))
    for tag in json.loads(merge.tags_json):
        if tag not in merged_tags:
            merged_tags.append(tag)
    keep.tags_json = json.dumps(merged_tags)

    merged_attrs = json.loads(keep.attributes_json)
    for key, value in json.loads(merge.attributes_json).items():
        merged_attrs.setdefault(key, value)
    keep.attributes_json = json.dumps(merged_attrs)

    keep.description = keep.description or merge.description
    keep.agent_notes = keep.agent_notes or merge.agent_notes
    keep.agent_summary = keep.agent_summary or merge.agent_summary

    db.execute(text("UPDATE entity_edges SET from_entity_id = :k WHERE from_entity_id = :m"), {"k": keep_id, "m": merge_id})
    db.execute(text("UPDATE entity_edges SET to_entity_id = :k WHERE to_entity_id = :m"), {"k": keep_id, "m": merge_id})
    db.execute(text("UPDATE entity_events SET entity_id = :k WHERE entity_id = :m"), {"k": keep_id, "m": merge_id})
    db.execute(text("UPDATE entity_history SET entity_id = :k WHERE entity_id = :m"), {"k": keep_id, "m": merge_id})

    for table, column in (("observations", "entity_ids_json"), ("patterns", "applies_to_entity_ids_json")):
        rows = db.execute(
            text(f"SELECT id, {column} FROM {table} WHERE {column} LIKE :pat"), {"pat": f"%{merge_id}%"}
        ).fetchall()
        for row in rows:
            ids = json.loads(getattr(row, column))
            if merge_id not in ids:
                continue
            new_ids, seen = [], set()
            for i in ids:
                mapped = keep_id if i == merge_id else i
                if mapped not in seen:
                    seen.add(mapped)
                    new_ids.append(mapped)
            db.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": json.dumps(new_ids), "id": row.id})

    db.delete(merge)
    db.commit()
    db.refresh(keep)

    with contextlib.suppress(Exception):
        delete_entity_embedding(merge_id)

    return keep
