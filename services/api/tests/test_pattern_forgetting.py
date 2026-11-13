"""A pattern can be deleted, and a deleted observation does not live on in patterns: its id
leaves them, its lineage links go, and a pattern left with no observation goes with it. A
restored backup replays the same deletes."""

from datetime import UTC, datetime

from app.core.db import Base
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.services import deletion_recovery as recovery
from memory_stack import stack as stack
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

WHEN = datetime(2026, 1, 2, tzinfo=UTC)


def _seed(db):
    db.add_all([
        Observation(id="obs_1", agent_id="owner", session_id="s", observed_at=WHEN, signal_type="mood",
                    description="tired after calls with Maya"),
        Observation(id="obs_2", agent_id="owner", session_id="s", observed_at=WHEN, signal_type="mood",
                    description="tired on Mondays", related_observation_ids_json='["obs_1"]'),
        Pattern(id="pat_only", agent_id="owner", pattern_name="calls with Maya",
                description="tired after calls with Maya", observation_ids_json='["obs_1"]'),
        Pattern(id="pat_both", agent_id="owner", pattern_name="tired", observation_ids_json='["obs_1", "obs_2"]'),
        Pattern(id="pat_other", agent_id="owner", pattern_name="other", observation_ids_json='["obs_2"]'),
        ObjectLink(id="lnk_1", source_type="observation", source_id="obs_1",
                   target_type="pattern", target_id="pat_both", relationship="supports"),
        ObjectLink(id="lnk_2", source_type="observation", source_id="obs_2",
                   target_type="pattern", target_id="pat_other", relationship="supports"),
    ])
    db.commit()


def _assert_forgotten(db):
    assert db.get(Observation, "obs_1") is None
    assert db.get(Pattern, "pat_only") is None  # its only observation is gone
    assert db.get(Pattern, "pat_both") is None  # its text may carry what obs_1 said
    assert db.get(Observation, "obs_2").related_observation_ids_json == "[]"
    assert db.get(Pattern, "pat_other") is None  # deleted by hand
    assert db.scalars(select(ObjectLink)).all() == []


def _forget(stack):
    headers = stack.admin_headers()
    assert stack.http.delete("/observation/obs_1", headers=headers).status_code == 200
    assert stack.http.delete("/pattern/pat_other", headers=headers).status_code == 200


def test_deleting_an_observation_and_a_pattern_leaves_nothing_that_names_them(stack):
    with stack.sessions() as db:
        _seed(db)

    _forget(stack)

    with stack.sessions() as db:
        _assert_forgotten(db)
    assert "calls with Maya" not in stack.stored_text()
    assert stack.http.delete("/pattern/pat_other", headers=stack.admin_headers()).status_code == 404


def test_a_pattern_of_another_agent_is_not_found(stack):
    with stack.sessions() as db:
        _seed(db)

    other = {**stack.admin_headers(), "X-Agent-Id": "helper"}

    assert stack.http.delete("/pattern/pat_other", headers=other).status_code == 404
    with stack.sessions() as db:
        assert db.get(Pattern, "pat_other") is not None


def test_a_restored_backup_replays_the_observation_and_pattern_deletes(stack, tmp_path):
    with stack.sessions() as db:
        _seed(db)
    _forget(stack)
    with stack.sessions() as db:
        receipts = recovery.evidence(db)
    assert {("owner", "pattern", p) for p in ("pat_only", "pat_both", "pat_other")} <= set(receipts)

    engine = create_engine(f"sqlite:///{tmp_path / 'restored.db'}")
    Base.metadata.create_all(engine)
    restored = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    with restored() as db:
        _seed(db)

    recovery.replay(receipts, restored)

    with restored() as db:
        _assert_forgotten(db)
    engine.dispose()
