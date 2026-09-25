"""Builds the structured pre-session briefing object (FIX 5).

Most of this leans on `attributes_json` (already a freeform blob on Entity)
for fields the schema doesn't have a dedicated column for yet (project
status, scheduled_for, warmth_level) - that's exactly what that column is
for, so no new columns are added for it.
"""
import json
from datetime import UTC, datetime, timedelta

from app.models.entity import Entity, EntityEvent
from app.models.memory import Memory
from app.models.observation import Observation
from sqlalchemy import select

RECENT_WINDOW_DAYS = 7
TASK_TOUCHED_WINDOW_DAYS = 21
STREAK_WINDOW_DAYS = 30
STREAK_THRESHOLD = 2
WATCH_FLAG_WINDOW_DAYS = 14
TOKEN_BUDGET = 300


def _now():
    return datetime.now(UTC)


def _attrs(entity: Entity) -> dict:
    try:
        return json.loads(entity.attributes_json)
    except (TypeError, ValueError):
        return {}


def _emotional_state(db, agent_id: str) -> tuple[str | None, str | None]:
    row = db.execute(
        select(Observation)
        .where(Observation.agent_id == agent_id, Observation.signal_type == "emotional")
        .order_by(Observation.observed_at.desc())
        .limit(1)
    ).scalars().first()

    if not row or not row.observed_at:
        return None, None

    if row.observed_at < _now() - timedelta(hours=24):
        return None, None

    return (row.hypothesis or None), (row.description or None)


def _active_streaks(db, agent_id: str) -> list[dict]:
    cutoff = _now() - timedelta(days=STREAK_WINDOW_DAYS)
    rows = db.execute(
        select(EntityEvent, Entity)
        .join(Entity, Entity.id == EntityEvent.entity_id)
        .where(Entity.agent_id == agent_id, EntityEvent.event_type == "streak", EntityEvent.created_at >= cutoff)
    ).all()

    counts: dict[str, int] = {}
    for event, _entity in rows:
        key = event.description or "streak"
        counts[key] = counts.get(key, 0) + 1

    return [{"name": name, "count": count} for name, count in counts.items() if count >= STREAK_THRESHOLD]


def _pending_clarifications(db, agent_id: str) -> list[dict]:
    # clarifications live on Observation now (needs_clarification=True); there's
    # no dedicated importance column post-merge, so hypothesis_confidence -
    # already "how sure are we this needs surfacing" - stands in for it.
    rows = db.execute(
        select(Observation)
        .where(Observation.agent_id == agent_id, Observation.needs_clarification == True, Observation.status == "unconfirmed")  # noqa: E712
        .order_by(Observation.hypothesis_confidence.desc(), Observation.observed_at.desc())
        .limit(2)
    ).scalars().all()

    return [
        {"id": row.id, "what": row.description, "trigger_condition": row.raise_condition}
        for row in rows
    ]


def _active_tasks(db, agent_id: str) -> list[dict]:
    cutoff = _now() - timedelta(days=TASK_TOUCHED_WINDOW_DAYS)
    rows = db.execute(
        select(Entity).where(Entity.agent_id == agent_id, Entity.entity_type == "project")
    ).scalars().all()

    tasks: dict[str, dict] = {}
    for entity in rows:
        attrs = _attrs(entity)
        if attrs.get("status") == "completed":
            continue

        last_event = db.execute(
            select(EntityEvent)
            .where(EntityEvent.entity_id == entity.id, EntityEvent.created_at >= cutoff)
            .order_by(EntityEvent.created_at.desc())
            .limit(1)
        ).scalars().first()
        if not last_event:
            continue

        # Same-topic tasks can still show up as separate entity rows (e.g.
        # duplicate entities not yet caught by the dedup migration) - key on
        # normalized name so the briefing never surfaces the same task twice,
        # keeping whichever copy has been stuck longer.
        key = " ".join(entity.name.strip().lower().split())
        candidate = {
            "entity_id": entity.id,
            "name": entity.name,
            "status": attrs.get("status", ""),
            "sessions_stuck": attrs.get("sessions_stuck", 0),
        }
        existing = tasks.get(key)
        if existing is None or candidate["sessions_stuck"] > existing["sessions_stuck"]:
            tasks[key] = candidate

    return sorted(tasks.values(), key=lambda t: t["sessions_stuck"], reverse=True)


def _people_relevant(db, agent_id: str) -> list[dict]:
    cutoff = _now() - timedelta(days=RECENT_WINDOW_DAYS)
    today_str = _now().date().isoformat()

    rows = db.execute(
        select(Entity).where(Entity.agent_id == agent_id, Entity.entity_type == "human")
    ).scalars().all()

    people = []
    for entity in rows:
        attrs = _attrs(entity)
        scheduled_today = attrs.get("scheduled_for") == today_str

        recent_event = db.execute(
            select(EntityEvent)
            .where(EntityEvent.entity_id == entity.id, EntityEvent.created_at >= cutoff)
            .order_by(EntityEvent.created_at.desc())
            .limit(1)
        ).scalars().first()

        if not (scheduled_today or recent_event):
            continue

        note = attrs.get("note") or (recent_event.description if recent_event else "")
        people.append({
            "entity_id": entity.id,
            "name": entity.name,
            "warmth_level": attrs.get("warmth_level", 0.5),
            "note": note,
        })

    return people


def _watch_flags(db, agent_id: str) -> list[str]:
    cutoff = _now() - timedelta(days=WATCH_FLAG_WINDOW_DAYS)
    rows = db.execute(
        select(Memory)
        .where(Memory.agent_id == agent_id, Memory.memory_type == "watch", Memory.created_at >= cutoff)
        .order_by(Memory.created_at.desc())
        .limit(5)
    ).scalars().all()
    return [row.summary or row.text[:120] for row in rows]


def _estimate_tokens(obj) -> int:
    return len(json.dumps(obj)) // 4


def _trim_to_budget(briefing: dict) -> dict:
    if _estimate_tokens(briefing) <= TOKEN_BUDGET:
        return briefing

    briefing["watch_flags"] = []
    if _estimate_tokens(briefing) <= TOKEN_BUDGET:
        return briefing

    briefing["people_relevant"] = []
    if _estimate_tokens(briefing) <= TOKEN_BUDGET:
        return briefing

    briefing["active_tasks"] = briefing["active_tasks"][:1]
    if _estimate_tokens(briefing) <= TOKEN_BUDGET:
        return briefing

    briefing["pending_clarifications"] = briefing["pending_clarifications"][:1]
    return briefing


def build_briefing(db, agent_id: str) -> dict:
    emotional_state, mood_summary = _emotional_state(db, agent_id)

    briefing = {
        "emotional_state": emotional_state,
        "mood_summary": mood_summary,
        "active_streaks": _active_streaks(db, agent_id),
        "pending_clarifications": _pending_clarifications(db, agent_id),
        "active_tasks": _active_tasks(db, agent_id),
        "people_relevant": _people_relevant(db, agent_id),
        "watch_flags": _watch_flags(db, agent_id),
    }

    return _trim_to_budget(briefing)
