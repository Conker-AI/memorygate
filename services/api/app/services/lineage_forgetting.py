"""Shared steps of a delete: drop the lineage links that name an object (each leaving a
content-free `link` receipt) and take its id out of other rows' JSON id lists."""

import json

from app.models.deletion_receipt import record
from app.models.object_link import ObjectLink
from sqlalchemy import or_, select, text


def erase_links(db, kind: str, object_id: str) -> None:
    """Delete every lineage link with `kind`/`object_id` at either end (no commit)."""
    links = db.scalars(select(ObjectLink).where(or_(
        (ObjectLink.source_type == kind) & (ObjectLink.source_id == object_id),
        (ObjectLink.target_type == kind) & (ObjectLink.target_id == object_id),
    ))).all()
    for link in links:
        record(db, "", "link", link.id)
        db.delete(link)


def _without(value: str | None, object_id: str) -> list | None:
    """The stored id list without `object_id`, or None when it does not hold it (or is not a list)."""
    try:
        ids = json.loads(value) if value else []
    except (TypeError, ValueError):
        return None
    if not isinstance(ids, list) or object_id not in ids:
        return None
    return [i for i in ids if i != object_id]


def strip_id(db, table: str, column: str, object_id: str) -> list[tuple[str, list]]:
    """Remove `object_id` from `table.column` (a JSON id list) in every row that holds it.
    Returns (row id, ids left) for each changed row. `table` and `column` are constants."""
    rows = db.execute(
        text(f"SELECT id, {column} AS ids FROM {table} WHERE {column} LIKE :pat"),
        {"pat": f"%{object_id}%"},
    ).fetchall()
    changed = []
    for row in rows:
        kept = _without(row.ids, object_id)
        if kept is not None:
            db.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"),
                       {"v": json.dumps(kept), "id": row.id})
            changed.append((row.id, kept))
    return changed
