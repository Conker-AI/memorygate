"""The contradiction detector flags opposing language instead of trusting a memory silently."""

import json
from datetime import UTC, datetime

import pytest
from app.core.db import Base
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from app.services import memory_truth
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_memory(db, identity, text, **kwargs):
    row = Memory(id=identity, agent_id=kwargs.pop("agent_id", "owner"), text=text, **kwargs)
    db.add(row)
    return row


def revisions(db, memory_id):
    return db.execute(select(MemoryRevision).where(MemoryRevision.memory_id == memory_id)).scalars().all()


def test_snapshot_returns_exactly_seven_keys_with_iso_dates_or_none(db):
    memory = add_memory(
        db, "dated", "Owner prefers quasar scheduling.", summary="scheduling preference",
        memory_type="fact", confidence="high", status="active",
        valid_from=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        valid_until=datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC),
    )
    blank = add_memory(db, "blank", "A short note.")
    db.flush()

    dated = memory_truth.snapshot(memory)
    blank = memory_truth.snapshot(blank)

    assert set(dated) == {"text", "summary", "memory_type", "confidence", "status", "valid_from", "valid_until"}
    assert dated["text"] == "Owner prefers quasar scheduling."
    assert dated["summary"] == "scheduling preference"
    assert dated["memory_type"] == "fact"
    assert dated["confidence"] == "high"
    assert dated["status"] == "active"
    assert dated["valid_from"] == "2026-01-02T03:04:05+00:00"
    assert dated["valid_until"] == "2026-03-04T05:06:07+00:00"
    assert blank["valid_from"] is None and blank["valid_until"] is None


def test_add_revision_stores_the_snapshot_under_system(db):
    memory = add_memory(db, "m1", "Owner prefers quasar scheduling.", confidence="high")
    db.flush()

    memory_truth.add_revision(db, memory, "review requested")

    (row,) = revisions(db, "m1")
    assert row.changed_by == "system"
    assert row.reason == "review requested"
    assert row.snapshot_json == json.dumps(memory_truth.snapshot(memory))


def test_opposing_negation_over_shared_terms_flags_both_memories(db):
    memory = add_memory(db, "new", "Owners prefer quasar scheduling.")
    other = add_memory(db, "old", "Owners no longer prefer quasar scheduling.")
    db.flush()

    created = memory_truth.detect_conflicts(db, memory)
    db.flush()

    assert len(created) == 1
    conflict = created[0]
    assert conflict.agent_id == "owner"
    assert conflict.memory_id == "new"
    assert conflict.conflicting_memory_id == "old"
    assert conflict.status == "open"
    # The detector counts how many of the new memory's long words (owners,
    # prefer, quasar, scheduling) appear in the other text: all four here.
    assert conflict.confidence == min(0.85, 0.35 + 4 * 0.1)
    assert memory.status == "needs_review"
    assert other.status == "needs_review"
    memory_revision = revisions(db, "new")
    other_revision = revisions(db, "old")
    assert [row.changed_by for row in memory_revision] == ["conflict_detector"]
    assert [row.changed_by for row in other_revision] == ["conflict_detector"]
    assert memory_revision[0].reason == "possible contradiction detected"
    assert other_revision[0].reason == "possible contradiction detected"


def test_no_conflict_for_same_polarity_other_agent_or_single_shared_word(db):
    same_polarity = add_memory(db, "sp1", "Owners prefer quasar scheduling.", agent_id="same")
    also_positive = add_memory(db, "sp2", "Owners really prefer quasar scheduling.", agent_id="same")
    other_agent = add_memory(db, "oa1", "Owners prefer quasar scheduling.", agent_id="one")
    elsewhere = add_memory(db, "oa2", "Owners no longer prefer quasar scheduling.", agent_id="two")
    single_word = add_memory(db, "sw1", "Quasar scheduling matters.")
    negated_single = add_memory(db, "sw2", "Quasar stopped mattering.")
    db.flush()

    assert memory_truth.detect_conflicts(db, same_polarity) == []
    assert memory_truth.detect_conflicts(db, other_agent) == []
    # Opposing polarity, but "quasar" is the only long word the two share.
    assert memory_truth.detect_conflicts(db, single_word) == []
    assert same_polarity.status == "active" and also_positive.status == "active"
    assert other_agent.status == "active" and elsewhere.status == "active"
    assert single_word.status == "active" and negated_single.status == "active"
    assert db.execute(select(MemoryConflict)).scalars().all() == []


def test_detecting_twice_does_not_duplicate_the_open_conflict(db):
    memory = add_memory(db, "new", "Owners prefer quasar scheduling.")
    other = add_memory(db, "old", "Owners no longer prefer quasar scheduling.")
    db.flush()
    memory_truth.detect_conflicts(db, memory)
    db.flush()
    # The old memory is trusted again while the conflict is still open, so the
    # second pass reaches it and must find the conflict it already recorded.
    other.status = "active"
    db.flush()

    assert memory_truth.detect_conflicts(db, memory) == []
    db.flush()

    conflicts = db.execute(select(MemoryConflict).where(MemoryConflict.status == "open")).scalars().all()
    assert len(conflicts) == 1
    assert len(revisions(db, "new")) == 1 and len(revisions(db, "old")) == 1


def test_text_without_words_longer_than_four_letters_is_not_checked(db):
    memory = add_memory(db, "new", "Do not cry out yet.")
    other = add_memory(db, "old", "We no longer cry out.")
    db.flush()

    assert memory_truth.detect_conflicts(db, memory) == []
    assert memory.status == "active" and other.status == "active"
    assert db.execute(select(MemoryConflict)).scalars().all() == []


def test_mark_unsupported_moves_only_active_memories(db):
    before = datetime.now(UTC)
    active = add_memory(db, "active", "Owner prefers quasar scheduling.")
    review = add_memory(db, "review", "Older quasar scheduling note.", status="needs_review",
                        valid_until=datetime(2026, 1, 1, tzinfo=UTC))
    db.flush()

    memory_truth.mark_unsupported(db, active, "no supporting observations")
    memory_truth.mark_unsupported(db, review, "no supporting observations")
    db.flush()

    assert active.status == "needs_review"
    assert active.valid_until is not None and active.valid_until >= before
    (revision,) = revisions(db, "active")
    assert revision.changed_by == "support_graph"
    assert revision.reason == "no supporting observations"
    assert review.status == "needs_review"
    assert review.valid_until == datetime(2026, 1, 1, tzinfo=UTC)
    assert revisions(db, "review") == []
