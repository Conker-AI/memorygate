import json
from datetime import UTC, datetime

from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from sqlalchemy import select


def snapshot(memory: Memory) -> dict:
    return {"text": memory.text, "summary": memory.summary, "memory_type": memory.memory_type,
            "confidence": memory.confidence, "status": memory.status,
            "valid_from": memory.valid_from.isoformat() if memory.valid_from else None,
            "valid_until": memory.valid_until.isoformat() if memory.valid_until else None}


def add_revision(db, memory: Memory, reason: str, changed_by: str = "system") -> None:
    db.add(MemoryRevision(memory_id=memory.id, changed_by=changed_by, reason=reason, snapshot_json=json.dumps(snapshot(memory))))


def detect_conflicts(db, memory: Memory) -> list[MemoryConflict]:
    """Conservative lexical conflict detector; ambiguous cases stay untouched."""
    lower = memory.text.lower()
    negated = any(token in lower for token in ("not ", "no longer", "never ", "stopped "))
    terms = [term for term in lower.replace(".", " ").split() if len(term) > 4][:8]
    if not terms:
        return []
    matches = db.execute(select(Memory).where(Memory.agent_id == memory.agent_id, Memory.id != memory.id, Memory.status == "active")).scalars().all()
    created = []
    for other in matches:
        other_lower = other.text.lower()
        overlap = sum(1 for term in terms if term in other_lower)
        other_negated = any(token in other_lower for token in ("not ", "no longer", "never ", "stopped "))
        if overlap < 2 or negated == other_negated:
            continue
        exists = db.execute(select(MemoryConflict).where(
            MemoryConflict.memory_id == memory.id,
            MemoryConflict.conflicting_memory_id == other.id,
            MemoryConflict.status == "open",
        )).scalar_one_or_none()
        if not exists:
            row = MemoryConflict(agent_id=memory.agent_id, memory_id=memory.id, conflicting_memory_id=other.id,
                                 reason="Opposing temporal language over overlapping terms", confidence=min(0.85, 0.35 + overlap * 0.1))
            db.add(row)
            # A contradiction is not silently promoted into agent context. Both
            # records remain auditable and require an explicit administrator decision.
            if memory.status == "active":
                memory.status = "needs_review"
                add_revision(db, memory, "possible contradiction detected", "conflict_detector")
            if other.status == "active":
                other.status = "needs_review"
                add_revision(db, other, "possible contradiction detected", "conflict_detector")
            created.append(row)
    return created


def mark_unsupported(db, memory: Memory, reason: str) -> None:
    if memory.status == "active":
        memory.status = "needs_review"
        memory.valid_until = datetime.now(UTC)
        add_revision(db, memory, reason, "support_graph")
