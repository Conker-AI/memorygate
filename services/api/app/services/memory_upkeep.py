"""Memory upkeep candidates (R-M4, read-only first step): what could be merged, resolved, retired
or checked, each with the ids involved and the reason. It changes nothing and deletes nothing;
the owner (through Pi or the CLI) decides what to do with each candidate.

One agent's memories only, the same rows recall can return (active or waiting for review, never
ones a forgotten conversation receipt names)."""

import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations

from app.models.conversation_receipt import ConversationReceipt
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError

MAX_SCAN = 5000  # newest memories read per request; `truncated` says when there were more
NEAR_SHARED = 0.8  # shared fraction of the larger word set that makes two memories near duplicates
COMMON_WORD = 50  # a word in more memories than this links too much to suggest duplicates
LIVE_STATUSES = ("active", "needs_review")
STAMP_EVERY = timedelta(hours=1)  # recall stamps a memory at most this often
logger = logging.getLogger(__name__)
_RIGHT_QUOTE = chr(0x2019)  # a typographic apostrophe counts as a plain one
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _normal(text: str) -> str:
    return " ".join(text.lower().replace(_RIGHT_QUOTE, "'").split())


def _words(text: str) -> frozenset[str]:
    return frozenset(word for word in _WORD.findall(_normal(text)) if len(word) > 3)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def stamp_recalled(db, memory_ids: list[str], now: datetime | None = None) -> None:
    """Record that recall returned these memories, in its own transaction after the answer's.

    Not an edit: the revision and `updated_at` stay as they were. Best effort: a row stamped in
    the last hour is skipped, and a failed stamp (a lock or deadlock between two recalls) is
    rolled back and dropped, so recall never fails because of it."""
    ids = sorted(set(memory_ids))
    if not ids:
        return
    now = now or datetime.now(UTC)
    try:
        db.execute(
            update(Memory)
            .where(Memory.id.in_(ids), or_(Memory.last_recalled_at.is_(None),
                                           Memory.last_recalled_at < now - STAMP_EVERY))
            .values(last_recalled_at=now, updated_at=Memory.updated_at)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("could not stamp last_recalled_at for %d memories", len(ids))


def _live_rows(db, agent_id: str):
    forgotten = select(ConversationReceipt.id).where(
        ConversationReceipt.agent_id == agent_id,
        ConversationReceipt.memory_id == Memory.id,
        ConversationReceipt.state == "deleted",
    ).exists()
    return db.execute(
        select(Memory.id, Memory.text, Memory.status, Memory.created_at, Memory.valid_until,
               Memory.review_by, Memory.last_recalled_at)
        .where(Memory.agent_id == agent_id, Memory.status.in_(LIVE_STATUSES), ~forgotten)
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .limit(MAX_SCAN + 1)
    ).all()


def _exact_duplicates(rows) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[_normal(row.text)].append(row.id)
    return [{"ids": sorted(ids), "reason": "same text once case and spacing are ignored"}
            for ids in groups.values() if len(ids) > 1]


def _near_duplicates(rows, exact: list[dict]) -> list[dict]:
    exact_ids = {memory_id for group in exact for memory_id in group["ids"]}
    words = {row.id: _words(row.text) for row in rows if row.id not in exact_ids}
    postings = defaultdict(list)
    for memory_id, terms in words.items():
        for term in terms:
            postings[term].append(memory_id)
    shared = defaultdict(int)
    for ids in postings.values():
        if len(ids) <= COMMON_WORD:
            for pair in combinations(sorted(ids), 2):
                shared[pair] += 1
    found = []
    for (first, second), count in shared.items():
        larger = max(len(words[first]), len(words[second]))
        if count >= 2 and count / larger >= NEAR_SHARED:
            found.append({"ids": [first, second],
                          "reason": f"{round(100 * count / larger)}% of their words are the same"})
    return sorted(found, key=lambda item: item["ids"])


def _contradictions(db, agent_id: str, live_ids: set[str]) -> list[dict]:
    rows = db.execute(select(MemoryConflict).where(
        MemoryConflict.agent_id == agent_id, MemoryConflict.status == "open",
        or_(MemoryConflict.memory_id.in_(live_ids), MemoryConflict.conflicting_memory_id.in_(live_ids)),
    )).scalars().all() if live_ids else []
    return [{"ids": [row.memory_id, row.conflicting_memory_id], "conflict_id": row.id,
             "reason": row.reason or "open conflict"}
            for row in rows if row.memory_id in live_ids and row.conflicting_memory_id in live_ids]


def _expired(rows, now: datetime) -> list[dict]:
    found = []
    for row in rows:
        valid_until, review_by = _as_utc(row.valid_until), _as_utc(row.review_by)
        if valid_until is not None and valid_until < now:
            found.append({"ids": [row.id], "reason": f"valid until {valid_until.date().isoformat()}"})
        elif review_by is not None and review_by < now:
            found.append({"ids": [row.id], "reason": f"review was due {review_by.date().isoformat()}"})
    return found


def _not_recalled(rows, now: datetime, days: int) -> list[dict]:
    cutoff = now - timedelta(days=days)
    found = []
    for row in rows:
        created, recalled = _as_utc(row.created_at), _as_utc(row.last_recalled_at)
        if created is None or created >= cutoff or row.status != "active":
            continue
        if recalled is None:
            found.append({"ids": [row.id], "reason": f"saved over {days} days ago and never recalled"})
        elif recalled < cutoff:
            found.append({"ids": [row.id], "reason": f"last recalled {recalled.date().isoformat()}"})
    return found


def _capped(items: list[dict], limit: int) -> dict:
    return {"items": items[:limit], "total": len(items)}


def upkeep_candidates(db, agent_id: str, *, unused_days: int, limit: int,
                      now: datetime | None = None) -> dict:
    """Candidates by kind; each item has `ids` and `reason`. Reads only."""
    now = now or datetime.now(UTC)
    rows = _live_rows(db, agent_id)
    truncated = len(rows) > MAX_SCAN
    rows = rows[:MAX_SCAN]
    exact = _exact_duplicates(rows)
    return {
        "agent_id": agent_id,
        "checked_at": now.isoformat(),
        "memories_checked": len(rows),
        "truncated": truncated,
        "exact_duplicates": _capped(exact, limit),
        "near_duplicates": _capped(_near_duplicates(rows, exact), limit),
        "contradictions": _capped(_contradictions(db, agent_id, {row.id for row in rows}), limit),
        "expired": _capped(_expired(rows, now), limit),
        "not_recalled": _capped(_not_recalled(rows, now, unused_days), limit),
    }
