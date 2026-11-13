"""Merging entities keeps lineage and the graph whole: the startup dedup and the manual merge
repoint lineage links, drop edges that would loop or repeat, queue the vector delete, and a
malformed JSON value never stops the merge (or startup)."""

from datetime import UTC, datetime, timedelta

from app.core.data_migrations import _merge_duplicate_entities
from app.models.entity import Entity, EntityEdge
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from memory_stack import stack as stack
from sqlalchemy import select

EARLY = datetime(2026, 1, 1, tzinfo=UTC)


def _seed(stack, keep_name="Maya Rowan", merge_name=" maya rowan", merge_tags='["friend"]'):
    with stack.sessions() as db:
        db.add_all([
            Entity(id="ent_keep", agent_id="owner", entity_type="human", name=keep_name,
                   tags_json='["colleague"]', created_at=EARLY),
            Entity(id="ent_dup", agent_id="owner", entity_type="human", name=merge_name,
                   tags_json=merge_tags, description="met at the release party",
                   created_at=EARLY + timedelta(days=1)),
            Entity(id="ent_other", agent_id="owner", entity_type="project", name="Release"),
            EntityEdge(id="edge_between", from_entity_id="ent_keep", to_entity_id="ent_dup", relationship_type="same"),
            EntityEdge(id="edge_kept", from_entity_id="ent_keep", to_entity_id="ent_other", relationship_type="works_on"),
            EntityEdge(id="edge_repeat", from_entity_id="ent_dup", to_entity_id="ent_other", relationship_type="works_on"),
            EntityEdge(id="edge_moved", from_entity_id="ent_other", to_entity_id="ent_dup", relationship_type="owned_by"),
            ObjectLink(id="lnk_about", source_type="observation", source_id="obs_one",
                       target_type="entity", target_id="ent_dup", relationship="about"),
            ObjectLink(id="lnk_between", source_type="entity", source_id="ent_keep",
                       target_type="entity", target_id="ent_dup", relationship="related_to"),
            Observation(id="obs_one", agent_id="owner", session_id="s", observed_at=EARLY,
                        signal_type="preference", description="Maya prefers paper checklists",
                        entity_ids_json='["ent_dup", "ent_other"]'),
        ])
        db.commit()


def _assert_folded(stack):
    with stack.sessions() as db:
        assert [e.id for e in db.execute(select(Entity).where(Entity.entity_type == "human")).scalars()] == ["ent_keep"]
        edges = {(e.from_entity_id, e.to_entity_id, e.relationship_type)
                 for e in db.execute(select(EntityEdge)).scalars()}
        assert edges == {("ent_keep", "ent_other", "works_on"), ("ent_other", "ent_keep", "owned_by")}
        links = [(link.source_id, link.target_id) for link in db.execute(select(ObjectLink)).scalars()]
        assert links == [("obs_one", "ent_keep")]
        assert db.get(Observation, "obs_one").entity_ids_json == '["ent_keep", "ent_other"]'
    deletes = [row for row in stack.outbox() if row["object_id"] == "ent_dup"]
    assert [(row["object_kind"], row["operation"]) for row in deletes] == [("entity", "delete")]


def test_startup_dedup_repoints_lineage_drops_loops_and_queues_the_vector_delete(stack):
    _seed(stack)

    with stack.engine.begin() as conn:
        _merge_duplicate_entities(conn)

    _assert_folded(stack)
    with stack.sessions() as db:
        keep = db.get(Entity, "ent_keep")
        assert keep.tags_json == '["colleague", "friend"]'
        assert keep.description == "met at the release party"


def test_a_malformed_tags_value_does_not_stop_the_startup_dedup(stack):
    _seed(stack, merge_tags="not json")

    with stack.engine.begin() as conn:
        _merge_duplicate_entities(conn)

    with stack.sessions() as db:
        assert db.get(Entity, "ent_dup") is None
        assert db.get(Entity, "ent_keep").tags_json == '["colleague"]'


def test_manual_merge_repoints_lineage_and_drops_loops(stack):
    _seed(stack, merge_name="Maya R.")

    merged = stack.http.post("/entity/merge", headers=stack.admin_headers(),
                             json={"keep_entity_id": "ent_keep", "merge_entity_id": "ent_dup"})

    assert merged.status_code == 200, merged.text
    _assert_folded(stack)
