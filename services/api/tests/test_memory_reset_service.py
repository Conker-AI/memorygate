"""Memory reset: a backup first, then knowledge and its linked history go; credentials and sources stay."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from app.core import auth
from app.core.db import Base
from app.models.agent_access_key import AgentAccessKey
from app.models.audit import MemoryAudit
from app.models.entity import Entity, EntityEdge, EntityEvent, EntityHistory
from app.models.evidence_source import EvidenceSource
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from app.models.vector_outbox import VectorOutbox
from app.routes import system as system_routes
from app.services import auth_settings_service, backup_service
from app.services.auth_settings_service import set_admin_key
from app.services.memory_reset_service import reset_memory
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ADMIN_KEY = "Admin-key-123!-long-enough"
OLD, NEW, CUTOFF = datetime(2026, 1, 1), datetime(2026, 10, 30), datetime(2026, 10, 1)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reset.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path / "backups"))
    yield factory
    engine.dispose()


def _seed(db, when=None):
    stamp = {} if when is None else {"created_at": when}
    tag = "new" if when is NEW else "old" if when is OLD else "x"
    db.add_all([
        Memory(id=f"m-{tag}", agent_id="owner", text=f"erase me {tag}", **stamp),
        Memory(id=f"m2-{tag}", agent_id="helper", text="another agent's memory", **stamp),
        Entity(id=f"e-{tag}", agent_id="owner", entity_type="person", name="Sam", **stamp),
        Entity(id=f"e2-{tag}", agent_id="owner", entity_type="place", name="Office", **stamp),
        Observation(id=f"o-{tag}", agent_id="owner", signal_type="verbal", description="said so",
                    **({} if when is None else {"observed_at": when})),
        MemoryAudit(action="memory_created", memory_id=f"m-{tag}", **stamp),
    ])
    db.add_all([
        MemoryRevision(memory_id=f"m-{tag}", reason="edit", snapshot_json="{}"),
        MemoryConflict(agent_id="owner", memory_id=f"m2-{tag}", conflicting_memory_id=f"m-{tag}"),
        EntityEdge(from_entity_id=f"e-{tag}", to_entity_id=f"e2-{tag}", relationship_type="works_at"),
        EntityEvent(entity_id=f"e-{tag}", event_type="met", description="met"),
        EntityHistory(entity_id=f"e-{tag}", changed_field="name"),
        ObjectLink(source_type="memory", source_id=f"m-{tag}", target_type="entity", target_id=f"e-{tag}",
                   relationship="about"),
    ])


def _survivors(db):
    db.add_all([
        EvidenceSource(source_key="mail", source_type="imap", label="Mail"),
        AgentAccessKey(label="pi", agent_id="owner", key_hash="hash"),
    ])


def test_a_full_reset_backs_up_first_then_removes_knowledge_and_linked_history(sessions, tmp_path):
    with sessions() as db:
        _seed(db)
        _survivors(db)
        db.commit()
        result = reset_memory(db)

    backup = Path(tmp_path / "backups" / result["backup"]["filename"])
    assert "erase me x" in backup.read_text()  # the backup holds what the reset removed
    assert result["reset_from"] is None
    assert result["removed"] == {"memories": 2, "entities": 2, "observations": 1, "patterns": 0, "evidence": 0,
                                 "analysis": 0, "episodes": 0, "transcripts": 0, "jobs": 0, "audit": 1}
    assert result["indexing"] == {"status": "pending", "operations": 5}
    with sessions() as db:
        for model in (Memory, Entity, Observation, MemoryAudit, MemoryRevision, MemoryConflict, EntityEdge,
                      EntityEvent, EntityHistory, ObjectLink):
            assert db.query(model).count() == 0, model.__name__
        # Sources and access credentials deliberately survive.
        assert db.query(EvidenceSource).count() == 1 and db.query(AgentAccessKey).count() == 1
        # Every removed vector point has a delete waiting in the outbox.
        queued = {(row.object_kind, row.object_id, row.operation) for row in db.scalars(select(VectorOutbox))}
    assert queued == {("memory", "m-x", "delete"), ("memory", "m2-x", "delete"), ("entity", "e-x", "delete"),
                      ("entity", "e2-x", "delete"), ("observation", "o-x", "delete")}


def test_a_reset_from_a_date_keeps_older_knowledge_and_its_history(sessions):
    with sessions() as db:
        _seed(db, OLD)
        _seed(db, NEW)
        # Links that cross the date: one end is removed, so the link goes too, whichever end it is.
        db.add_all([
            ObjectLink(source_type="memory", source_id="m-old", target_type="entity", target_id="e-new",
                       relationship="about"),
            ObjectLink(source_type="memory", source_id="m-new", target_type="entity", target_id="e-old",
                       relationship="about"),
            EntityEdge(from_entity_id="e-old", to_entity_id="e-new", relationship_type="knows"),
            EntityEdge(from_entity_id="e-new", to_entity_id="e-old", relationship_type="knows"),
            MemoryConflict(agent_id="owner", memory_id="m-old", conflicting_memory_id="m-new"),
            MemoryConflict(agent_id="owner", memory_id="m-new", conflicting_memory_id="m-old"),
        ])
        db.commit()
        result = reset_memory(db, CUTOFF)

    assert result["reset_from"] == CUTOFF.isoformat()
    assert result["removed"]["memories"] == 2 and result["removed"]["entities"] == 2
    # Observations have no created_at, so a dated reset keeps every one of them rather than guess.
    assert result["removed"]["observations"] == 0 and result["indexing"]["operations"] == 4
    with sessions() as db:
        assert sorted(row.id for row in db.scalars(select(Memory))) == ["m-old", "m2-old"]
        assert sorted(row.id for row in db.scalars(select(Entity))) == ["e-old", "e2-old"]
        assert [row.memory_id for row in db.scalars(select(MemoryRevision))] == ["m-old"]
        assert [(row.from_entity_id, row.to_entity_id) for row in db.scalars(select(EntityEdge))] == [("e-old", "e2-old")]
        assert [(row.source_id, row.target_id) for row in db.scalars(select(ObjectLink))] == [("m-old", "e-old")]
        conflicts = [(row.memory_id, row.conflicting_memory_id) for row in db.scalars(select(MemoryConflict))]
        assert conflicts == [("m2-old", "m-old")]
        assert [row.memory_id for row in db.scalars(select(MemoryAudit))] == ["m-old"]
        assert sorted(row.id for row in db.scalars(select(Observation))) == ["o-new", "o-old"]


def test_resetting_an_empty_workspace_still_writes_a_backup(sessions, tmp_path):
    with sessions() as db:
        result = reset_memory(db)
    assert set(result["removed"].values()) == {0}
    assert result["indexing"]["operations"] == 0
    assert json.loads((tmp_path / "backups" / result["backup"]["filename"]).read_text())["tables"]["memories"] == []


# --- The route refuses before anything is touched ----------------------------


def _client(sessions, monkeypatch):
    for module in (auth, system_routes):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    monkeypatch.setattr(auth_settings_service, "_attempt_state", {})
    with sessions() as db:
        set_admin_key(db, ADMIN_KEY)
        _seed(db)
        db.commit()
    app = FastAPI()
    app.include_router(system_routes.router, dependencies=[Depends(auth.require_key)])
    return TestClient(app, raise_server_exceptions=False)


def _reset(client, current_key):
    return client.post("/system/memory-reset", headers={"X-MemoryGate-Key": ADMIN_KEY},
                       json={"current_key": current_key, "confirmation": system_routes.MEMORY_RESET_PHRASE})


def test_a_wrong_current_key_deletes_nothing_and_writes_no_backup(sessions, monkeypatch, tmp_path):
    client = _client(sessions, monkeypatch)
    response = _reset(client, "not-the-admin-key")
    assert response.status_code == 403
    assert not (tmp_path / "backups").exists()
    with sessions() as db:
        assert db.query(Memory).count() == 2


def test_five_wrong_keys_lock_the_reset_even_for_the_right_key(sessions, monkeypatch):
    client = _client(sessions, monkeypatch)
    codes = [_reset(client, f"wrong-{n}").status_code for n in range(5)]
    assert codes == [403, 403, 403, 403, 429]
    assert _reset(client, ADMIN_KEY).status_code == 429
    with sessions() as db:
        assert db.query(Memory).count() == 2
