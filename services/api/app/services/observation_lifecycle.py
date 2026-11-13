"""Dedup, exposure tracking, and active-observation budget enforcement (FIX 3)."""
import re
from datetime import UTC, datetime

from app.models.observation import Observation
from app.services.qdrant_store import INDEX_UNREACHABLE, find_similar_observations, semantic_status
from sqlalchemy import func, select

DEDUP_SIMILARITY_THRESHOLD = 0.85
# Common words longer than three letters that say nothing about a trigger's topic.
TRIGGER_STOPWORDS = frozenset({
    "about", "been", "from", "have", "that", "their", "there", "they",
    "this", "were", "what", "when", "will", "with", "would", "your",
})


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
    """Archive the most-exposed unconfirmed observations until one more fits under the cap.

    A cap below one (an old stored value) counts as one. Runs inside the caller's transaction:
    the archive is committed together with the observation being created, or not at all.
    """
    cap = max(1, max_observations)
    unconfirmed = (Observation.agent_id == agent_id, Observation.status == "unconfirmed")
    excess = db.scalar(select(func.count()).select_from(Observation).where(*unconfirmed)) - cap + 1
    if excess <= 0:
        return

    # Only the rows to archive are loaded: the most exposed, oldest first among equals.
    victims = db.execute(
        select(Observation).where(*unconfirmed)
        .order_by(Observation.exposure_count.desc(), Observation.observed_at.asc(), Observation.id.asc())
        .limit(excess)
    ).scalars().all()
    now = datetime.now(UTC)
    for victim in victims:
        victim.status = "archived"
        victim.archived_at = now
        victim.archive_reason = "max active observations reached; archived to make room"
    db.flush()


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
    context_words = set(re.findall(r"\w+", lower_context))
    exposed_ids = []
    archived_ids = []

    for row in rows:
        trigger_lower = row.trigger_context.lower()
        trigger_words = {
            w for w in re.findall(r"\w+", trigger_lower) if len(w) > 3 and w not in TRIGGER_STOPWORDS
        }
        phrase = re.search(r"(?<!\w)" + re.escape(trigger_lower) + r"(?!\w)", lower_context)
        matched = phrase is not None or not trigger_words.isdisjoint(context_words)
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
