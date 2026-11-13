"""Lineage links built from the legacy JSON arrays are backfilled once, not at every start:
a link the owner deleted stays deleted, and the arrays stay the record forgetting reads."""

from datetime import UTC, datetime

from app.core import data_migrations
from app.core.data_migrations import _backfill_object_links
from app.models.entity import Entity
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from memory_stack import OWNER
from memory_stack import stack as stack
from sqlalchemy import inspect, select, text

WHEN = datetime(2026, 1, 2, tzinfo=UTC)


def _prepare(stack, monkeypatch):
    monkeypatch.setattr(data_migrations, "_table_exists", lambda conn, name: inspect(conn).has_table(name))
    with stack.engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_object_links_path "
            "ON object_links (source_type, source_id, target_type, target_id, relationship)"
        ))


def _observation(obs_id: str, entity_id: str) -> Observation:
    return Observation(id=obs_id, agent_id=OWNER, session_id="s", observed_at=WHEN, signal_type="verbal",
                       description="x", entity_ids_json=f'["{entity_id}"]')


def _backfill(stack):
    with stack.engine.begin() as conn:
        _backfill_object_links(conn)


def _about_links(db, obs_id: str):
    return db.scalars(select(ObjectLink).where(
        ObjectLink.source_id == obs_id, ObjectLink.relationship == "about",
    )).all()


def test_a_deleted_backfilled_link_does_not_come_back_at_the_next_start(stack, monkeypatch):
    _prepare(stack, monkeypatch)
    with stack.sessions() as db:
        db.add_all([Entity(id="ent_e", agent_id=OWNER, entity_type="human", name="Maya"),
                    _observation("obs_o", "ent_e")])
        db.commit()
    _backfill(stack)
    # The backfill's 'about' link carries swapped end types (source_type 'entity'), so the
    # observation's lineage view does not list it; the owner deletes it by its id.
    with stack.sessions() as db:
        (link,) = _about_links(db, "obs_o")
        link_id = link.id

    deleted = stack.http.delete(f"/lineage/links/{link_id}", headers=stack.admin_headers())
    _backfill(stack)

    assert deleted.status_code == 200
    with stack.sessions() as db:
        assert _about_links(db, "obs_o") == []
        assert db.get(Observation, "obs_o").entity_ids_json == '["ent_e"]'


def test_an_install_that_already_backfilled_is_marked_and_not_backfilled_again(stack, monkeypatch):
    _prepare(stack, monkeypatch)
    with stack.sessions() as db:
        db.add_all([
            Entity(id="ent_e", agent_id=OWNER, entity_type="human", name="Maya"),
            ObjectLink(id="lnk_old", source_type="observation", source_id="obs_old", target_type="entity",
                       target_id="ent_e", relationship="about", created_by="migration"),
            _observation("obs_new", "ent_e"),
        ])
        db.commit()
    with stack.engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS data_migration_steps (name TEXT PRIMARY KEY)"))
        conn.execute(text("DELETE FROM data_migration_steps"))

    _backfill(stack)

    with stack.sessions() as db:
        assert _about_links(db, "obs_new") == []
    with stack.engine.begin() as conn:
        marker = conn.execute(text(
            "SELECT 1 FROM data_migration_steps WHERE name = 'object_links_backfill'"
        )).first()
    assert marker is not None
