"""Safe, portable logical backups for MemoryGate's user memory data."""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from app.core.config import BACKUP_DIR
from app.models.analysis_object import AnalysisObject
from app.models.conversation_receipt import ConversationReceipt
from app.models.deletion_receipt import DeletionReceipt, RecoveryHold
from app.models.entity import Entity, EntityEdge, EntityEvent, EntityHistory
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.evidence_source import EvidenceSource
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.models.processing_job import ProcessingJob
from app.models.session_transcript import SessionTranscript
from sqlalchemy import select

_MODELS = (Memory, Entity, EntityEdge, EntityEvent, EntityHistory, Observation, Pattern,
           SessionTranscript, EvidenceSource, EvidenceObject, EpisodeObject, AnalysisObject,
           ObjectLink, ProcessingJob, MemoryRevision, MemoryConflict, ConversationReceipt,
           DeletionReceipt, RecoveryHold)


def _value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _rows(db, model):
    # Credential tables are intentionally not included. Listener source secrets are
    # also omitted so a stolen backup cannot be used as an ingest credential.
    excluded = {"secret_json"} if model is EvidenceSource else set()
    return [{column.name: _value(getattr(row, column.name)) for column in model.__table__.columns if column.name not in excluded}
            for row in db.execute(select(model)).scalars().all()]


def create_backup(db) -> dict:
    destination = Path(BACKUP_DIR)
    destination.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "memorygate-logical-backup-v1",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "notes": "Admin/read-key hashes and listener secrets are intentionally excluded.",
        "tables": {model.__tablename__: _rows(db, model) for model in _MODELS},
    }
    encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    filename = f"memorygate-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}.json"
    (destination / filename).write_bytes(encoded)
    return {"filename": filename, "size": len(encoded), "sha256": digest, "created_at": payload["created_at"]}


def list_backups() -> list[dict]:
    destination = Path(BACKUP_DIR)
    if not destination.exists():
        return []
    files = []
    for path in sorted(destination.glob("memorygate-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        files.append({"filename": path.name, "size": path.stat().st_size, "created_at": datetime.utcfromtimestamp(path.stat().st_mtime).isoformat() + "Z"})
    return files


def resolve_backup(filename: str) -> Path:
    candidate = Path(filename).name
    path = Path(BACKUP_DIR) / candidate
    if not candidate.startswith("memorygate-") or path.suffix != ".json" or not path.is_file():
        raise FileNotFoundError(candidate)
    return path
