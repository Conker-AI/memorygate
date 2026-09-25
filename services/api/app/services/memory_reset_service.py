"""Admin-only destructive reset operations for the single MemoryGate workspace."""
import contextlib
from datetime import datetime

from app.models.analysis_object import AnalysisObject
from app.models.audit import MemoryAudit
from app.models.entity import Entity, EntityEdge, EntityEvent, EntityHistory
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.models.processing_job import ProcessingJob
from app.models.session_transcript import SessionTranscript
from app.services.backup_service import create_backup
from app.services.qdrant_store import delete_embeddings


def _rows_for_reset(db, model, reset_from: datetime | None):
    query = db.query(model)
    if reset_from is not None and hasattr(model, "created_at"):
        query = query.filter(model.created_at >= reset_from)
    elif reset_from is not None:
        return []
    return query.all()


def reset_memory(db, reset_from: datetime | None = None) -> dict:
    """Create a logical backup, then remove knowledge data and its vector points.

    Evidence-source configuration and all access credentials deliberately survive.
    """
    backup = create_backup(db)
    selected = {
        "memories": _rows_for_reset(db, Memory, reset_from),
        "entities": _rows_for_reset(db, Entity, reset_from),
        "observations": _rows_for_reset(db, Observation, reset_from),
        "patterns": _rows_for_reset(db, Pattern, reset_from),
        "evidence": _rows_for_reset(db, EvidenceObject, reset_from),
        "analysis": _rows_for_reset(db, AnalysisObject, reset_from),
        "episodes": _rows_for_reset(db, EpisodeObject, reset_from),
        "transcripts": _rows_for_reset(db, SessionTranscript, reset_from),
        "jobs": _rows_for_reset(db, ProcessingJob, reset_from),
        "audit": _rows_for_reset(db, MemoryAudit, reset_from),
    }
    memory_ids = [row.id for row in selected["memories"]]
    entity_ids = [row.id for row in selected["entities"]]
    observation_ids = [row.id for row in selected["observations"]]
    all_selected_ids = set(memory_ids + entity_ids + observation_ids)
    all_selected_ids.update(row.id for rows in selected.values() for row in rows if hasattr(row, "id"))

    # Linked history must not survive as orphaned records after a reset.
    for model, field in ((MemoryRevision, "memory_id"), (MemoryConflict, "memory_id"), (MemoryConflict, "conflicting_memory_id"),
                         (EntityHistory, "entity_id"), (EntityEvent, "entity_id"), (EntityEdge, "from_entity_id"),
                         (EntityEdge, "to_entity_id")):
        if all_selected_ids:
            db.query(model).filter(getattr(model, field).in_(all_selected_ids)).delete(synchronize_session=False)
    if all_selected_ids:
        db.query(ObjectLink).filter(
            ObjectLink.source_id.in_(all_selected_ids) | ObjectLink.target_id.in_(all_selected_ids)
        ).delete(synchronize_session=False)

    for rows in selected.values():
        for row in rows:
            db.delete(row)
    db.commit()
    # Postgres remains the source of truth. A later reset or startup can repair vector state.
    with contextlib.suppress(Exception):
        delete_embeddings(memory_ids, observation_ids, entity_ids)
    removed = {name: len(rows) for name, rows in selected.items()}
    return {"backup": backup, "removed": removed, "reset_from": reset_from.isoformat() if reset_from else None}
