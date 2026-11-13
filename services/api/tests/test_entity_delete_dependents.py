"""Deleting an entity takes what refers to it: history snapshots (which hold its old name and
notes), events, edges, lineage links, and its id in observation and pattern id lists. A restored
backup replays the same delete."""

from datetime import UTC, datetime

from app.core.db import Base
from app.models.entity import Entity, EntityEdge, EntityEvent, EntityHistory
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
        Entity(id="ent_maya", agent_id="owner", entity_type="human", name="Maya Rowan",
               agent_notes="lives on Finch Street"),
        Entity(id="ent_release", agent_id="owner", entity_type="project", name="Release"),
        EntityHistory(id="his_1", entity_id="ent_maya", changed_field="agent_notes",
                      old_value_json='"lives on Finch Street"', new_value_json='"moved"'),
        EntityEvent(id="evt_1", entity_id="ent_maya", event_type="move", description="moved to Finch Street"),
        EntityEdge(id="edg_1", from_entity_id="ent_maya", to_entity_id="ent_release", relationship_type="works_on"),
        EntityEdge(id="edg_2", from_entity_id="ent_release", to_entity_id="ent_maya", relationship_type="owned_by"),
        ObjectLink(id="lnk_1", source_type="observation", source_id="obs_1",
                   target_type="entity", target_id="ent_maya", relationship="about"),
        Observation(id="obs_1", agent_id="owner", session_id="s", observed_at=WHEN, signal_type="preference",
                    description="prefers paper", entity_ids_json='["ent_maya", "ent_release"]'),
        Pattern(id="pat_1", agent_id="owner", pattern_name="paper", applies_to_entity_ids_json='["ent_maya"]'),
    ])
    db.commit()


def _assert_nothing_left(db):
    assert db.get(Entity, "ent_maya") is None
    assert db.scalars(select(EntityHistory)).all() == []
    assert db.scalars(select(EntityEvent)).all() == []
    assert db.scalars(select(EntityEdge)).all() == []
    assert db.scalars(select(ObjectLink)).all() == []
    assert db.get(Observation, "obs_1").entity_ids_json == '["ent_release"]'
    assert db.get(Pattern, "pat_1").applies_to_entity_ids_json == "[]"
    assert db.get(Entity, "ent_release") is not None


def test_deleting_an_entity_removes_its_history_events_edges_links_and_ids(stack):
    with stack.sessions() as db:
        _seed(db)

    deleted = stack.http.delete("/entity/ent_maya", headers=stack.admin_headers())

    assert deleted.status_code == 200, deleted.text
    with stack.sessions() as db:
        _assert_nothing_left(db)
    assert "Finch Street" not in stack.stored_text()


def test_a_restored_backup_replays_the_entity_delete_with_its_dependents(stack, tmp_path):
    with stack.sessions() as db:
        _seed(db)
    stack.http.delete("/entity/ent_maya", headers=stack.admin_headers())
    with stack.sessions() as db:
        receipts = recovery.evidence(db)
    assert ("owner", "entity", "ent_maya") in receipts

    restored_engine = create_engine(f"sqlite:///{tmp_path / 'restored.db'}")
    Base.metadata.create_all(restored_engine)
    restored = sessionmaker(restored_engine, autoflush=False, expire_on_commit=False)
    with restored() as db:
        _seed(db)

    recovery.replay(receipts, restored)

    with restored() as db:
        _assert_nothing_left(db)
    restored_engine.dispose()
