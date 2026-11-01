"""Direct tests for the pattern promotion service."""

import json

from app.core.db import Base
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.services.pattern_promotion import (
    MAX_PATTERN_CONFIDENCE,
    clamp_confidence,
    maybe_promote,
    promote_from_observations,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'x.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, sessions


def test_three_confirmed_observations_become_one_candidate_pattern(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            # Create observations with similar hypotheses
            obs1 = Observation(
                id="obs1",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="Late  Night work",
                status="confirmed",
                entity_ids_json="[]",
            )
            obs2 = Observation(
                id="obs2",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis=" late night   WORK ",
                status="confirmed",
                entity_ids_json='["a","b"]',
            )
            obs3 = Observation(
                id="obs3",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="late night work",
                status="confirmed",
                entity_ids_json='["b","c"]',
            )
            db.add(obs1)
            db.add(obs2)
            db.commit()

            # With only first two, no pattern should be created
            patterns = promote_from_observations(db, "owner")
            assert patterns == []
            assert db.scalar(select(Pattern)) is None

            # Add third observation
            db.add(obs3)
            db.commit()

            # Now a pattern should be created
            patterns = promote_from_observations(db, "owner")
            assert len(patterns) == 1

            pattern = patterns[0]
            assert pattern.pattern_name == "verbal: late night work"
            assert pattern.status == "candidate"
            assert pattern.confirmation_count == 1
            assert pattern.instance_count == 3
            assert pattern.confidence == 0.5
            assert json.loads(pattern.applies_to_entity_ids_json) == ["a", "b", "c"]
            assert pattern.interpretation == "Late  Night work"
            assert pattern.description.startswith("Auto-promoted from 3 observations")
    finally:
        engine.dispose()


def test_rerunning_only_confirms_when_the_cluster_grew(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            # Create the initial three observations
            obs1 = Observation(
                id="obs1",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="late night work",
                status="confirmed",
                entity_ids_json='["a","b"]',
            )
            obs2 = Observation(
                id="obs2",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="late night work",
                status="confirmed",
                entity_ids_json='["b","c"]',
            )
            obs3 = Observation(
                id="obs3",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="late night work",
                status="confirmed",
                entity_ids_json='["c","d"]',
            )
            db.add_all([obs1, obs2, obs3])
            db.commit()

            # Create the initial pattern
            patterns = promote_from_observations(db, "owner")
            assert len(patterns) == 1
            pattern = patterns[0]
            assert pattern.confirmation_count == 1

            # Rerunning should not increase confirmation_count
            patterns = promote_from_observations(db, "owner")
            assert len(patterns) == 1
            assert patterns[0].confirmation_count == 1

            # Add a fourth observation
            obs4 = Observation(
                id="obs4",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="late night work",
                status="confirmed",
                entity_ids_json='["d","e"]',
            )
            db.add(obs4)
            db.commit()

            # Now confirmation_count should increase
            patterns = promote_from_observations(db, "owner")
            assert len(patterns) == 1
            assert patterns[0].confirmation_count == 2
            assert patterns[0].instance_count == 4

            # Still only one Pattern row
            all_patterns = db.execute(select(Pattern)).scalars().all()
            assert len(all_patterns) == 1
    finally:
        engine.dispose()


def test_which_observations_qualify(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            # Three qualifying observations (unconfirmed with confirmation_count=2)
            obs1 = Observation(
                id="obs1",
                agent_id="owner",
                signal_type="tonal",
                description="d",
                hypothesis="x",
                status="unconfirmed",
                confirmation_count=2,
                entity_ids_json="[]",
            )
            obs2 = Observation(
                id="obs2",
                agent_id="owner",
                signal_type="tonal",
                description="d",
                hypothesis="x",
                status="unconfirmed",
                confirmation_count=2,
                entity_ids_json="[]",
            )
            obs3 = Observation(
                id="obs3",
                agent_id="owner",
                signal_type="tonal",
                description="d",
                hypothesis="x",
                status="unconfirmed",
                confirmation_count=2,
                entity_ids_json="[]",
            )
            db.add_all([obs1, obs2, obs3])
            db.commit()

            # Should create one pattern
            patterns = promote_from_observations(db, "owner")
            assert len(patterns) == 1
            assert patterns[0].pattern_name == "tonal: x"

            # Now test non-qualifying observations
            obs4 = Observation(
                id="obs4",
                agent_id="owner",
                signal_type="timing",
                description="d",
                hypothesis="y",
                status="unconfirmed",
                confirmation_count=1,
                entity_ids_json="[]",
            )
            obs5 = Observation(
                id="obs5",
                agent_id="owner",
                signal_type="timing",
                description="d",
                hypothesis="y",
                status="unconfirmed",
                confirmation_count=1,
                entity_ids_json="[]",
            )
            obs6 = Observation(
                id="obs6",
                agent_id="owner",
                signal_type="timing",
                description="d",
                hypothesis="y",
                status="confirmed",
                entity_ids_json="[]",
            )
            db.add_all([obs4, obs5, obs6])
            db.commit()

            # Only one qualifies (obs6), so no pattern should be created for "y"
            patterns = promote_from_observations(db, "owner")
            # Should still only have the first pattern
            assert len(patterns) == 1
            assert patterns[0].pattern_name == "tonal: x"
            names = db.execute(select(Pattern.pattern_name)).scalars().all()
            assert names == ["tonal: x"]
    finally:
        engine.dispose()


def test_empty_hypotheses_never_cluster(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            # Three confirmed observations with empty hypotheses
            for i in range(3):
                obs = Observation(
                    id=f"obs{i}",
                    agent_id="owner",
                    signal_type="verbal",
                    description="d",
                    hypothesis="",
                    status="confirmed",
                    entity_ids_json="[]",
                )
                db.add(obs)
            db.commit()

            # No pattern should be created
            patterns = promote_from_observations(db, "owner")
            assert patterns == []
    finally:
        engine.dispose()


def test_signal_type_filter_and_agent_scope(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            # Create verbal observations for owner
            obs1 = Observation(
                id="obs1",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="test",
                status="confirmed",
                entity_ids_json="[]",
            )
            obs2 = Observation(
                id="obs2",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="test",
                status="confirmed",
                entity_ids_json="[]",
            )
            obs3 = Observation(
                id="obs3",
                agent_id="owner",
                signal_type="verbal",
                description="d",
                hypothesis="test",
                status="confirmed",
                entity_ids_json="[]",
            )
            db.add_all([obs1, obs2, obs3])
            db.commit()

            # Filter by signal_type="tonal" should ignore verbal observations
            patterns = promote_from_observations(db, "owner", signal_type="tonal")
            assert patterns == []

            # Without the filter the same three do cluster
            patterns = promote_from_observations(db, "owner")
            assert [p.pattern_name for p in patterns] == ["verbal: test"]
    finally:
        engine.dispose()


def test_other_agents_observations_never_join_the_owner_cluster(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            # Two owner observations plus one from another agent: three in
            # total, so only a scope leak could make a cluster of three.
            for obs_id, agent in [("o1", "owner"), ("o2", "owner"), ("x1", "other")]:
                db.add(
                    Observation(
                        id=obs_id,
                        agent_id=agent,
                        signal_type="tonal",
                        description="d",
                        hypothesis="test",
                        status="confirmed",
                        entity_ids_json="[]",
                    )
                )
            db.commit()

            assert promote_from_observations(db, "owner") == []
            assert db.scalar(select(Pattern)) is None
    finally:
        engine.dispose()


def test_long_hypotheses_are_named_by_their_first_60_characters(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            # Three confirmed observations with long hypothesis
            long_hypothesis = "z" * 100
            for i in range(3):
                obs = Observation(
                    id=f"obs{i}",
                    agent_id="owner",
                    signal_type="behavioral",
                    description="d",
                    hypothesis=long_hypothesis,
                    status="confirmed",
                    entity_ids_json="[]",
                )
                db.add(obs)
            db.commit()

            patterns = promote_from_observations(db, "owner")
            assert len(patterns) == 1
            expected_name = "behavioral: " + "z" * 60
            assert patterns[0].pattern_name == expected_name
    finally:
        engine.dispose()


def _pattern(status, confirmations=0, contradictions=0):
    return Pattern(
        status=status,
        confirmation_count=confirmations,
        contradiction_count=contradictions,
    )


def test_maybe_promote():
    pattern = _pattern("candidate", confirmations=4)
    maybe_promote(pattern)
    assert pattern.status == "candidate"
    assert pattern.promoted_at is None

    pattern = _pattern("candidate", confirmations=5)
    maybe_promote(pattern)
    assert pattern.status == "active"
    assert pattern.promoted_at is not None

    pattern = _pattern("active", contradictions=2)
    maybe_promote(pattern)
    assert pattern.status == "active"

    pattern = _pattern("active", contradictions=3)
    maybe_promote(pattern)
    assert pattern.status == "deprecated"

    pattern = _pattern("candidate", confirmations=5, contradictions=3)
    maybe_promote(pattern)
    assert pattern.status == "deprecated"

    pattern = _pattern("deprecated", confirmations=9)
    maybe_promote(pattern)
    assert pattern.status == "deprecated"


def test_confidence_can_never_reach_certainty():
    assert clamp_confidence(1.0) == 0.95
    assert clamp_confidence(0.95) == 0.95
    assert clamp_confidence(0.4) == 0.4
    assert MAX_PATTERN_CONFIDENCE < 1
