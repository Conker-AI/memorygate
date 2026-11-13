"""What goes with a deleted observation or pattern. Used by the DELETE routes and by recovery
replay, so a delete made before a restore leaves nothing behind after it either.

A pattern is built from observations: when the last observation it names is deleted, the
pattern goes too (with its own receipt), so its derived text does not outlive its evidence.
A pattern that still has other observations keeps its text."""

from app.models.deletion_receipt import record
from app.models.pattern import Pattern
from app.services.lineage_forgetting import erase_links, strip_id


def erase_pattern(db, pattern_id: str) -> None:
    """Delete the pattern's lineage links and the row itself, if present (no commit).
    The caller records the pattern's receipt."""
    erase_links(db, "pattern", pattern_id)
    row = db.get(Pattern, pattern_id)
    if row is not None:
        db.delete(row)


def erase_observation_dependents(db, observation_id: str) -> None:
    """Delete what refers to `observation_id` (no commit): its lineage links, its id in other
    observations' related ids, and its id in patterns; a pattern left with no observation goes."""
    erase_links(db, "observation", observation_id)
    strip_id(db, "observations", "related_observation_ids_json", observation_id)
    for pattern_id, left in strip_id(db, "patterns", "observation_ids_json", observation_id):
        if not left:
            pattern = db.get(Pattern, pattern_id)
            record(db, pattern.agent_id if pattern else "", "pattern", pattern_id)
            erase_pattern(db, pattern_id)
