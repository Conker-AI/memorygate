"""Dedup check for entity creation - exact name match first, then embedding
similarity, scoped to (agent_id, entity_type) so e.g. a person and a project
that happen to share a name never collide."""
import json

from app.models.entity import Entity
from app.services.qdrant_store import (
    INDEX_UNREACHABLE,
    find_similar_entities,
    semantic_status,
)
from app.services.vector_outbox import enqueue_delete
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


def _loads(value: str | None, empty):
    """A stored JSON list or object; a malformed or empty value reads as empty, never raises."""
    try:
        parsed = json.loads(value) if value else empty
    except (TypeError, ValueError):
        return empty
    return parsed if isinstance(parsed, type(empty)) else empty


def _repoint_edges(db, keep_id: str, merge_id: str) -> None:
    """Edges of `merge_id` move to `keep_id`; one between the two would become a self-loop and
    goes, and an edge that now repeats one `keep_id` already has (same ends and type) goes too."""
    db.execute(text(
        "DELETE FROM entity_edges WHERE (from_entity_id = :m AND to_entity_id IN (:k, :m)) "
        "OR (to_entity_id = :m AND from_entity_id = :k)"
    ), {"k": keep_id, "m": merge_id})
    db.execute(text(
        "DELETE FROM entity_edges WHERE from_entity_id = :m AND EXISTS (SELECT 1 FROM entity_edges e "
        "WHERE e.from_entity_id = :k AND e.to_entity_id = entity_edges.to_entity_id "
        "AND e.relationship_type = entity_edges.relationship_type)"
    ), {"k": keep_id, "m": merge_id})
    db.execute(text(
        "DELETE FROM entity_edges WHERE to_entity_id = :m AND EXISTS (SELECT 1 FROM entity_edges e "
        "WHERE e.to_entity_id = :k AND e.from_entity_id = entity_edges.from_entity_id "
        "AND e.relationship_type = entity_edges.relationship_type)"
    ), {"k": keep_id, "m": merge_id})
    db.execute(text("UPDATE entity_edges SET from_entity_id = :k WHERE from_entity_id = :m"), {"k": keep_id, "m": merge_id})
    db.execute(text("UPDATE entity_edges SET to_entity_id = :k WHERE to_entity_id = :m"), {"k": keep_id, "m": merge_id})


def _repoint_links(db, keep_id: str, merge_id: str) -> None:
    """Lineage links to or from `merge_id` move to `keep_id`, so lineage outlives the merge.
    A link between the two goes; one `keep_id` already has goes (links are unique per ends
    and relationship)."""
    ids = {"k": keep_id, "m": merge_id}
    db.execute(text(
        "DELETE FROM object_links WHERE source_type = 'entity' AND target_type = 'entity' "
        "AND ((source_id = :m AND target_id IN (:k, :m)) OR (target_id = :m AND source_id = :k))"
    ), ids)
    for end, other in (("source", "target"), ("target", "source")):
        db.execute(text(
            f"DELETE FROM object_links WHERE {end}_type = 'entity' AND {end}_id = :m AND EXISTS "
            f"(SELECT 1 FROM object_links o WHERE o.{end}_type = 'entity' AND o.{end}_id = :k "
            f"AND o.{other}_type = object_links.{other}_type AND o.{other}_id = object_links.{other}_id "
            "AND o.relationship = object_links.relationship)"
        ), ids)
        db.execute(text(
            f"UPDATE object_links SET {end}_id = :k WHERE {end}_type = 'entity' AND {end}_id = :m"
        ), ids)


def _repoint_id_lists(db, keep_id: str, merge_id: str) -> None:
    for table, column in (("observations", "entity_ids_json"), ("patterns", "applies_to_entity_ids_json")):
        rows = db.execute(
            text(f"SELECT id, {column} FROM {table} WHERE {column} LIKE :pat"), {"pat": f"%{merge_id}%"}
        ).fetchall()
        for row in rows:
            ids = _loads(getattr(row, column), [])
            if merge_id not in ids:
                continue
            new_ids, seen = [], set()
            for i in ids:
                mapped = keep_id if i == merge_id else i
                if mapped not in seen:
                    seen.add(mapped)
                    new_ids.append(mapped)
            db.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": json.dumps(new_ids), "id": row.id})


def absorb_entity(db, keep: Entity, merge: Entity) -> None:
    """Fold `merge` into `keep` inside the caller's transaction (no commit): merged tags,
    attributes and empty text fields, every reference repointed (edges, events, history,
    lineage links, observation and pattern id lists), `merge`'s vector queued for deletion
    and its row deleted. Used by the manual merge and the startup dedup."""
    merged_tags = list(_loads(keep.tags_json, []))
    for tag in _loads(merge.tags_json, []):
        if tag not in merged_tags:
            merged_tags.append(tag)
    keep.tags_json = json.dumps(merged_tags)

    merged_attrs = dict(_loads(keep.attributes_json, {}))
    for key, value in _loads(merge.attributes_json, {}).items():
        merged_attrs.setdefault(key, value)
    keep.attributes_json = json.dumps(merged_attrs)

    keep.description = keep.description or merge.description
    keep.agent_notes = keep.agent_notes or merge.agent_notes
    keep.agent_summary = keep.agent_summary or merge.agent_summary

    _repoint_edges(db, keep.id, merge.id)
    db.execute(text("UPDATE entity_events SET entity_id = :k WHERE entity_id = :m"), {"k": keep.id, "m": merge.id})
    db.execute(text("UPDATE entity_history SET entity_id = :k WHERE entity_id = :m"), {"k": keep.id, "m": merge.id})
    _repoint_links(db, keep.id, merge.id)
    _repoint_id_lists(db, keep.id, merge.id)

    enqueue_delete(db, "entity", merge.id, agent_id=merge.agent_id)
    db.delete(merge)
    db.flush()


def merge_duplicate_groups(db) -> int:
    """Fold every group of same-named entities (per agent and type, lower(trim(name))) into its
    oldest row; the startup dedup. Flushes, never commits. Returns how many rows went."""
    folded = func.lower(func.trim(Entity.name))
    keys = db.execute(
        select(Entity.agent_id, Entity.entity_type, folded)
        .group_by(Entity.agent_id, Entity.entity_type, folded)
        .having(func.count() > 1)
    ).all()
    removed = 0
    for agent_id, entity_type, name in keys:
        rows = db.execute(
            select(Entity)
            .where(Entity.agent_id == agent_id, Entity.entity_type == entity_type, folded == name)
            .order_by(Entity.created_at, Entity.id)
        ).scalars().all()
        for merge in rows[1:]:
            absorb_entity(db, rows[0], merge)
            removed += 1
    return removed


def merge_entities(db, agent_id: str, keep_id: str, merge_id: str) -> Entity:
    """Manual merge for the graph's 'select two nodes -> merge' action: one caller-chosen pair
    of the caller's agent, committed."""
    if keep_id == merge_id:
        raise ValueError("cannot merge an entity into itself")

    keep = db.get(Entity, keep_id)
    merge = db.get(Entity, merge_id)
    if not keep or keep.agent_id != agent_id or not merge or merge.agent_id != agent_id:
        raise ValueError("entity not found")

    absorb_entity(db, keep, merge)
    db.commit()
    db.refresh(keep)

    return keep
