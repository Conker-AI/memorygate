"""Id lists written from a request name only rows that exist, for the same agent.

A deleted id written back into a pattern or an observation would point at nothing and, after a
restore, at a row a deletion receipt removes again; so a request naming one is refused."""

from fastapi import HTTPException
from sqlalchemy import select


def require_known(db, model, agent_id: str, ids: list[str] | None, field: str) -> None:
    """Refuse (422) when any of `ids` is not a row of `model` owned by `agent_id`."""
    wanted = list(dict.fromkeys(ids or []))
    if not wanted:
        return
    found = set(db.scalars(select(model.id).where(model.agent_id == agent_id, model.id.in_(wanted))))
    missing = [object_id for object_id in wanted if object_id not in found]
    if missing:
        raise HTTPException(422, {"code": "unknown_ids", "field": field, "ids": missing[:20]})
