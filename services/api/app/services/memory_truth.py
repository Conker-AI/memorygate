import json
import re
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


# Memories in the owner's own words (an approved statement, or what they said in a chat).
OWNER_SOURCES = {"user", "owner_statement"}
_NEGATIONS = {"not", "never", "stopped", "cannot"}
_APOSTROPHE = chr(0x2019)  # right single quotation mark, as in don't typed on a phone
_WORD = re.compile(r"[^\W_]+(?:['" + _APOSTROPHE + r"][^\W_]+)*")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _negated(words: list[str]) -> bool:
    joined = " ".join(words)
    return (any(word in _NEGATIONS or word.endswith(("n't", "n" + _APOSTROPHE + "t")) for word in words)
            or " no longer " in f" {joined} ")


def _long_terms(words: list[str]) -> set[str]:
    return {word for word in words if len(word) > 4}


def detect_conflicts(db, memory: Memory) -> list[MemoryConflict]:
    """Conservative lexical conflict detector; ambiguous cases stay untouched.

    Two memories conflict when exactly one of them negates ("not", "never", "no longer",
    "stopped", "-n't") and they share at least two whole words longer than four letters.
    Both go to review, except that a memory in the owner's own words is never taken out of
    recall because of one that is not: then only the other one goes to review.
    """
    words = _words(memory.text)
    negated = _negated(words)
    terms = _long_terms(words)
    if not terms:
        return []
    matches = db.execute(select(Memory).where(Memory.agent_id == memory.agent_id, Memory.id != memory.id, Memory.status == "active")).scalars().all()
    created = []
    for other in matches:
        other_words = _words(other.text)
        overlap = len(terms & _long_terms(other_words))
        if overlap < 2 or negated == _negated(other_words):
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
            owner_new, owner_other = memory.source_type in OWNER_SOURCES, other.source_type in OWNER_SOURCES
            if memory.status == "active" and not (owner_new and not owner_other):
                memory.status = "needs_review"
                add_revision(db, memory, "possible contradiction detected", "conflict_detector")
            if other.status == "active" and not (owner_other and not owner_new):
                other.status = "needs_review"
                add_revision(db, other, "possible contradiction detected", "conflict_detector")
            created.append(row)
    return created


def mark_unsupported(db, memory: Memory, reason: str) -> None:
    if memory.status == "active":
        memory.status = "needs_review"
        memory.valid_until = datetime.now(UTC)
        add_revision(db, memory, reason, "support_graph")
