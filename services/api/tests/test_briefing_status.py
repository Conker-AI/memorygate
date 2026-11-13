"""The briefing shows only live evidence.

A watch memory that left recall (needs_review after losing a conflict or being marked
unsupported) and an observation the owner contradicted or that was archived must not
reach the pre-session briefing either.
"""

from datetime import UTC, datetime, timedelta

import pytest
from app.core.db import Base
from app.models.memory import Memory
from app.models.observation import Observation
from app.services import briefing
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'briefing.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        yield session
    engine.dispose()


def naive_now() -> datetime:
    # SQLite returns stored datetimes without a zone; compare like with like.
    return datetime.now(UTC).replace(tzinfo=None)


def test_watch_flags_skip_memories_that_left_recall(db):
    now = datetime.now(UTC)
    db.add(Memory(
        agent_id="owner", memory_type="watch", status="needs_review",
        summary="old watch", text="old watch", created_at=now - timedelta(hours=2),
    ))
    db.add(Memory(
        agent_id="owner", memory_type="watch", status="active",
        summary="live watch", text="live watch", created_at=now - timedelta(hours=1),
    ))
    db.commit()

    assert briefing._watch_flags(db, "owner") == ["live watch"]


@pytest.mark.parametrize("dropped_status", ["contradicted", "archived"])
def test_emotional_state_skips_contradicted_and_archived_observations(db, monkeypatch, dropped_status):
    monkeypatch.setattr(briefing, "_now", naive_now)
    now = naive_now()
    db.add(Observation(
        agent_id="owner", signal_type="emotional", description="d", status="unconfirmed",
        hypothesis="calm", observed_at=now - timedelta(hours=3),
    ))
    db.add(Observation(
        agent_id="owner", signal_type="emotional", description="d", status=dropped_status,
        hypothesis="stressed", observed_at=now - timedelta(hours=1),
    ))
    db.commit()

    assert briefing._emotional_state(db, "owner")[0] == "calm"
