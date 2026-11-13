"""What goes with a deleted entity: its history snapshots, events, edges, lineage links, and
its id in observation and pattern id lists. Used by DELETE /entity/{id} and by recovery replay,
so an entity deleted before a restore leaves nothing behind after it either."""

import json

from app.models.deletion_receipt import record
from app.models.entity import EntityEdge, EntityEvent, EntityHistory
from app.models.object_link import ObjectLink
from sqlalchemy import delete, or_, select, text

ID_LISTS = (("observations", "entity_ids_json"), ("patterns", "applies_to_entity_ids_json"))


def _without(value: str | None, entity_id: str) -> list | None:
    """The stored id list without `entity_id`, or None when it does not hold it (or is not a list)."""
    try:
        ids = json.loads(value) if value else []
    except (TypeError, ValueError):
        return None
    if not isinstance(ids, list) or entity_id not in ids:
        return None
    return [i for i in ids if i != entity_id]


def erase_dependents(db, entity_id: str) -> None:
    """Delete everything that refers to `entity_id`, inside the caller's transaction (no commit).
    Each removed lineage link leaves a content-free `link` receipt, as other deletes do."""
    db.execute(delete(EntityHistory).where(EntityHistory.entity_id == entity_id))
    db.execute(delete(EntityEvent).where(EntityEvent.entity_id == entity_id))
    db.execute(delete(EntityEdge).where(
        or_(EntityEdge.from_entity_id == entity_id, EntityEdge.to_entity_id == entity_id)))

    links = db.scalars(select(ObjectLink).where(or_(
        (ObjectLink.source_type == "entity") & (ObjectLink.source_id == entity_id),
        (ObjectLink.target_type == "entity") & (ObjectLink.target_id == entity_id),
    ))).all()
    for link in links:
        record(db, "", "link", link.id)
        db.delete(link)

    for table, column in ID_LISTS:
        rows = db.execute(
            text(f"SELECT id, {column} AS ids FROM {table} WHERE {column} LIKE :pat"),
            {"pat": f"%{entity_id}%"},
        ).fetchall()
        for row in rows:
            kept = _without(row.ids, entity_id)
            if kept is not None:
                db.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"),
                           {"v": json.dumps(kept), "id": row.id})
