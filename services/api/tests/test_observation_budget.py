"""The active-observation budget and the exposure tracker bound unconfirmed observations.

`enforce_budget` archives the most-exposed unconfirmed observation of one agent once that
agent's unconfirmed count reaches the limit. `apply_session_context` counts an exposure for
every unconfirmed, trigger-backed observation whose trigger appears in the session context,
and archives one that reaches `max_exposures` without being confirmed.
"""

from datetime import UTC, datetime

import pytest
from app.core.db import Base
from app.models.observation import Observation
from app.services import observation_lifecycle
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

BUDGET_REASON = "max active observations reached; archived to make room"
EXPOSURE_REASON = "max exposures without confirmation"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'observations.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        yield session
    engine.dispose()


def observe(db, agent_id="owner", **overrides) -> Observation:
    row = Observation(
        agent_id=agent_id, signal_type="verbal", description="keeps interrupting", **overrides
    )
    db.add(row)
    db.commit()
    return row


def statuses_by_id(db) -> dict[str, str]:
    return {row.id: row.status for row in db.scalars(select(Observation))}


def test_below_the_limit_nothing_is_archived(db):
    low = observe(db, exposure_count=1)
    high = observe(db, exposure_count=4)

    observation_lifecycle.enforce_budget(db, "owner", 3)

    statuses = statuses_by_id(db)
    assert statuses == {low.id: "unconfirmed", high.id: "unconfirmed"}
    for row in db.scalars(select(Observation)):
        assert row.archived_at is None
        assert row.archive_reason is None


def test_at_the_limit_the_most_exposed_observation_is_archived(db):
    quiet = observe(db, exposure_count=1)
    middle = observe(db, exposure_count=3)
    loud = observe(db, exposure_count=7)

    observation_lifecycle.enforce_budget(db, "owner", 3)

    victim = db.get(Observation, loud.id)
    assert victim.status == "archived"
    assert victim.archived_at is not None
    assert victim.archive_reason == BUDGET_REASON
    assert db.get(Observation, quiet.id).status == "unconfirmed"
    assert db.get(Observation, middle.id).status == "unconfirmed"


def test_another_agents_observations_are_never_counted_or_archived(db):
    mine = observe(db, exposure_count=1)
    theirs = [observe(db, agent_id="other", exposure_count=9) for _ in range(6)]

    observation_lifecycle.enforce_budget(db, "owner", 3)

    assert db.get(Observation, mine.id).status == "unconfirmed"
    for row in theirs:
        assert db.get(Observation, row.id).status == "unconfirmed"
        assert row.archived_at is None


def test_observations_with_other_statuses_are_outside_the_budget(db):
    first = observe(db, exposure_count=1)
    second = observe(db, exposure_count=2)
    confirmed = observe(db, status="confirmed", confirmed_by="owner", exposure_count=9)
    contradicted = observe(db, status="contradicted", exposure_count=9)

    observation_lifecycle.enforce_budget(db, "owner", 3)

    # Two unconfirmed rows: below the limit, so nothing is archived even though the
    # confirmed and contradicted rows would push the count past it if they were counted.
    statuses = statuses_by_id(db)
    assert statuses == {
        first.id: "unconfirmed",
        second.id: "unconfirmed",
        confirmed.id: "confirmed",
        contradicted.id: "contradicted",
    }
    assert confirmed.archived_at is None
    assert contradicted.archived_at is None


def test_a_trigger_phrase_in_the_context_counts_one_exposure(db):
    row = observe(db, trigger_context="budget planning")

    result = observation_lifecycle.apply_session_context(
        db, "owner", "we had a BUDGET planning call"
    )

    assert result == {"exposed": [row.id], "archived": []}
    assert db.get(Observation, row.id).exposure_count == 1


def test_a_long_shared_word_counts_but_short_words_do_not(db):
    long_word = observe(db, trigger_context="standup notes")
    short_words = observe(db, trigger_context="red fox run")

    observation_lifecycle.apply_session_context(db, "owner", "missed standup today; the fox ran")

    assert db.get(Observation, long_word.id).exposure_count == 1
    assert db.get(Observation, short_words.id).exposure_count == 0


def test_reaching_max_exposures_archives_and_one_below_does_not(db):
    exhausting = observe(db, trigger_context="standup notes", exposure_count=2, max_exposures=3)
    surviving = observe(db, trigger_context="standup notes", exposure_count=1, max_exposures=3)

    result = observation_lifecycle.apply_session_context(db, "owner", "standup notes are due")

    exhausted = db.get(Observation, exhausting.id)
    assert exhausted.status == "archived"
    assert exhausted.exposure_count == 3
    assert exhausted.archived_at is not None
    assert exhausted.archive_reason == EXPOSURE_REASON
    survivor = db.get(Observation, surviving.id)
    assert survivor.status == "unconfirmed"
    assert survivor.exposure_count == 2
    assert survivor.archived_at is None
    assert set(result["exposed"]) == {exhausting.id, surviving.id}
    assert result["archived"] == [exhausting.id]


def test_empty_trigger_context_another_agent_and_archived_rows_are_untouched(db):
    no_trigger = observe(db, trigger_context="", exposure_count=4, max_exposures=5)
    other_agent = observe(db, agent_id="other", trigger_context="standup notes")
    already = observe(
        db,
        status="archived",
        trigger_context="standup notes",
        archived_at=datetime.now(UTC),
        archive_reason="manually archived",
    )

    result = observation_lifecycle.apply_session_context(db, "owner", "standup notes are due")

    assert result == {"exposed": [], "archived": []}
    untouched = db.get(Observation, no_trigger.id)
    assert untouched.status == "unconfirmed"
    assert untouched.exposure_count == 4
    assert db.get(Observation, other_agent.id).exposure_count == 0
    assert db.get(Observation, already.id).exposure_count == 0
