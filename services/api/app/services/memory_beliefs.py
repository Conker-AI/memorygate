"""What Conker believes about the owner (R-M5): a read-only list of one agent's current memories,
each with who said it (the owner or Conker), how sure it is, how much evidence backs it, where it
came from and which other beliefs disagree with it.

A belief is a memory recall can return (active or waiting for review, never one a forgotten
conversation receipt names) of a belief type (`skill` is the skills store). Nothing here writes,
not even the recall stamp: a list is not a recall. Fixing or forgetting a belief goes through the
correction routes with the `revision` given here. Design: context/design/memory-beliefs.md."""

import base64
import binascii
import json
import re
from datetime import UTC, datetime

from app.models.conversation_receipt import ConversationReceipt
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.object_link import ObjectLink
from app.services.memory_truth import OWNER_SOURCES
from sqlalchemy import and_, case, func, or_, select

BELIEF_TYPES = ("fact", "phase", "context", "watch")
LIVE_STATUSES = ("active", "needs_review")
LINEAGE_LIMIT = 5  # newest sources of support named per belief
_OWNER = tuple(sorted(OWNER_SOURCES))
# The memory-id shape the correction routes accept; anything else in a cursor is not ours.
_MEMORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")


class InvalidCursor(ValueError):
    """The `after` cursor was not one this route handed out."""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _cursor(row) -> str:
    raw = json.dumps([_iso(row.created_at), row.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(after: str) -> tuple[datetime, str]:
    try:
        created, memory_id = json.loads(base64.urlsafe_b64decode(after + "=" * (-len(after) % 4)))
        moment = datetime.fromisoformat(created)
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as error:
        raise InvalidCursor("invalid cursor") from error
    if not isinstance(memory_id, str) or not _MEMORY_ID.fullmatch(memory_id):
        raise InvalidCursor("invalid cursor")
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC), memory_id


def _is_stated():
    return Memory.source_type.in_(_OWNER)


def _belief_rows(agent_id: str) -> list:
    forgotten = select(ConversationReceipt.id).where(
        ConversationReceipt.agent_id == agent_id,
        ConversationReceipt.memory_id == Memory.id,
        ConversationReceipt.state == "deleted",
    ).exists()
    return [Memory.agent_id == agent_id, Memory.status.in_(LIVE_STATUSES),
            Memory.memory_type.in_(BELIEF_TYPES), ~forgotten]


def _filters(agent_id: str, origin: str, state: str, memory_type: str | None) -> list:
    where = _belief_rows(agent_id)
    if origin == "stated":
        where.append(_is_stated())
    elif origin == "concluded":
        where.append(or_(Memory.source_type.is_(None), ~_is_stated()))
    if state != "all":
        where.append(Memory.status == ("active" if state == "current" else "needs_review"))
    if memory_type:
        where.append(Memory.memory_type == memory_type)
    return where


def _counts(db, where: list) -> dict:
    stated = case((_is_stated(), 1), else_=0)
    review = case((Memory.status == "needs_review", 1), else_=0)
    total, owner, waiting = db.execute(
        select(func.count(Memory.id), func.coalesce(func.sum(stated), 0),
               func.coalesce(func.sum(review), 0)).where(*where)
    ).one()
    total, owner = int(total), int(owner)
    return {"total": total, "counts": {"stated": owner, "concluded": total - owner,
                                       "needs_review": int(waiting)}}


def _support(db, ids: list[str]) -> tuple[dict, dict]:
    """Support counts and the newest sources of support per memory. Invalidated support keeps its
    link with confidence 0, the same rule evidence invalidation uses."""
    links = db.execute(
        select(ObjectLink.target_id, ObjectLink.source_type, ObjectLink.source_id, ObjectLink.confidence)
        .where(ObjectLink.target_type == "memory", ObjectLink.relationship == "supports",
               ObjectLink.target_id.in_(ids))
        .order_by(ObjectLink.created_at.desc(), ObjectLink.id.desc())
    ).all()
    counts = {memory_id: {"supporting": 0, "withdrawn": 0} for memory_id in ids}
    lineage: dict[str, list] = {memory_id: [] for memory_id in ids}
    for link in links:
        withdrawn = not (link.confidence or 0) > 0
        counts[link.target_id]["withdrawn" if withdrawn else "supporting"] += 1
        if len(lineage[link.target_id]) < LINEAGE_LIMIT:
            lineage[link.target_id].append(
                {"object_type": link.source_type, "object_id": link.source_id, "withdrawn": withdrawn})
    return counts, lineage


def _conflicts(db, agent_id: str, ids: list[str]) -> dict:
    """Open conflicts naming a listed memory whose other side is also a belief."""
    rows = db.execute(
        select(MemoryConflict.id, MemoryConflict.memory_id, MemoryConflict.conflicting_memory_id)
        .where(MemoryConflict.agent_id == agent_id, MemoryConflict.status == "open",
               or_(MemoryConflict.memory_id.in_(ids), MemoryConflict.conflicting_memory_id.in_(ids)))
        .order_by(MemoryConflict.created_at, MemoryConflict.id)
    ).all()
    named = {row.memory_id for row in rows} | {row.conflicting_memory_id for row in rows}
    believed = set(db.scalars(select(Memory.id).where(Memory.id.in_(named), *_belief_rows(agent_id))))
    found: dict[str, list] = {memory_id: [] for memory_id in ids}
    for row in rows:
        for mine, other in ((row.memory_id, row.conflicting_memory_id),
                            (row.conflicting_memory_id, row.memory_id)):
            if mine in found and other in believed:
                found[mine].append({"conflict_id": row.id, "memory_id": other})
    return found


def _item(row, support: dict, lineage: list, conflicts: list) -> dict:
    return {
        "id": row.id,
        "revision": row.revision,
        "text": row.text,
        "memory_type": row.memory_type,
        "origin": "stated" if row.source_type in OWNER_SOURCES else "concluded",
        "source_type": row.source_type,
        "confidence": row.confidence,
        "state": "current" if row.status == "active" else "needs_review",
        "valid_from": _iso(row.valid_from),
        "valid_until": _iso(row.valid_until),
        "review_by": _iso(row.review_by),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "last_recalled_at": _iso(row.last_recalled_at),
        "evidence": support,
        "lineage": lineage,
        "conflicts": conflicts,
    }


def list_beliefs(db, agent_id: str, *, origin: str = "all", state: str = "all",
                 memory_type: str | None = None, limit: int = 50, after: str | None = None) -> dict:
    """One page of beliefs, newest first, keyset-paged on (created_at, id) so a correction between
    pages never moves a row. `total` and `counts` cover every page under the same filters."""
    where = _filters(agent_id, origin, state, memory_type)
    summary = _counts(db, where)
    page_where = list(where)
    if after:
        moment, memory_id = _decode(after)
        # The cursor row's own stored time when it still exists (exact on every store, whatever
        # precision the database kept); the cursor's copy only when it was forgotten meanwhile.
        stored = select(Memory.created_at).where(Memory.agent_id == agent_id, Memory.id == memory_id)
        mark = func.coalesce(stored.scalar_subquery(), moment)
        page_where.append(or_(Memory.created_at < mark,
                              and_(Memory.created_at == mark, Memory.id < memory_id)))
    rows = db.scalars(
        select(Memory).where(*page_where)
        .order_by(Memory.created_at.desc(), Memory.id.desc()).limit(limit + 1)
    ).all()
    more, rows = len(rows) > limit, rows[:limit]
    ids = [row.id for row in rows]
    support, lineage = _support(db, ids) if ids else ({}, {})
    conflicts = _conflicts(db, agent_id, ids) if ids else {}
    return {
        "agent_id": agent_id,
        **summary,
        "items": [_item(row, support[row.id], lineage[row.id], conflicts[row.id]) for row in rows],
        "next_after": _cursor(rows[-1]) if more else None,
    }
