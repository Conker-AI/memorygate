"""Dedup, exposure tracking, and active-observation budget enforcement (FIX 3)."""
from datetime import UTC, datetime

from app.models.observation import Observation
from app.services.qdrant_store import INDEX_UNREACHABLE, find_similar_observations, semantic_status
from sqlalchemy import select

DEDUP_SIMILARITY_THRESHOLD = 0.85


def find_duplicate(agent_id: str, signal_type: str, description: str) -> tuple[dict | None, dict]:
    """Return the duplicate, if any, and the status of the check.

    Observation dedup is purely vector-based, so without a vector path there is
    no duplicate signal at all - "None" here means "not checked", and the caller
    says so instead of implying the observation is known to be new.
    """
    status = semantic_status()
    if status["status"] != "ok":
        return None, status
    try:
        hits = find_similar_observations(description, agent_id=agent_id, signal_type=signal_type, limit=1)
    except Exception:
        return None, {"status": "degraded", "component": "vector_index", "reason": INDEX_UNREACHABLE}
    if hits and hits[0]["score"] >= DEDUP_SIMILARITY_THRESHOLD:
        return hits[0], status
    return None, status


def enforce_budget(db, agent_id: str, max_observations: int) -> None:
    active = db.execute(
        select(Observation).where(
            Observation.agent_id == agent_id,
            Observation.status == "unconfirmed",
        )
    ).scalars().all()

    if len(active) < max_observations:
        return

    victim = max(active, key=lambda o: o.exposure_count)
    victim.status = "archived"
    victim.archived_at = datetime.now(UTC)
    victim.archive_reason = "max active observations reached; archived to make room"
    db.commit()


def apply_session_context(db, agent_id: str, session_context: str) -> dict:
    """Increments exposure_count on unconfirmed observations whose trigger_context
    matches the given session context text, archiving any that hit max_exposures."""
    rows = db.execute(
        select(Observation).where(
            Observation.agent_id == agent_id,
            Observation.status == "unconfirmed",
            Observation.trigger_context != "",
        )
    ).scalars().all()

    lower_context = session_context.lower()
    exposed_ids = []
    archived_ids = []

    for row in rows:
        trigger_lower = row.trigger_context.lower()
        trigger_words = [w for w in trigger_lower.split() if len(w) > 3]
        matched = trigger_lower in lower_context or any(w in lower_context for w in trigger_words)
        if not matched:
            continue

        row.exposure_count += 1
        exposed_ids.append(row.id)

        if row.exposure_count >= row.max_exposures and row.status == "unconfirmed":
            row.status = "archived"
            row.archived_at = datetime.now(UTC)
            row.archive_reason = "max exposures without confirmation"
            archived_ids.append(row.id)

    db.commit()
    return {"exposed": exposed_ids, "archived": archived_ids}
