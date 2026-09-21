"""Revision-aware editing and atomic audit on an isolated database."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from app.core.db import Base
from app.models.memory import Memory
from app.models.audit import MemoryAudit
from app.models.memory_revision import MemoryRevision
from app.routes import memory

@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'revisions.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False)
    monkeypatch.setattr(memory, "SessionLocal", sessions)
    monkeypatch.setattr(memory, "index_after_commit", lambda *a, **kw: {"status": "ok"})
    monkeypatch.setattr(memory, "delete_memory_embedding", lambda *a: None)
    with sessions() as db:
        db.add(Memory(id="one", agent_id="owner", text="Original"))
        db.commit()
    app = FastAPI()
    app.include_router(memory.router)
    app.dependency_overrides[memory.get_agent_id] = lambda: "owner"
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, sessions
    engine.dispose()

def test_stale_edit_and_delete_preserve_newer_memory(setup):
    http, sessions = setup
    assert http.get("/memory/one").json()["revision"] == 1
    response = http.patch("/memory/one", json={"text": "Newer", "expected_revision": 1})
    assert response.status_code == 200
    assert response.json()["memory"]["revision"] == 2
    assert http.patch("/memory/one", json={"text": "Stale", "expected_revision": 1}).status_code == 409
    assert http.delete("/memory/one?expected_revision=1").status_code == 409
    with sessions() as db:
        assert db.get(Memory, "one").text == "Newer"
        assert db.scalar(select(func.count()).select_from(MemoryAudit)) == 1
        assert db.scalar(select(func.count()).select_from(MemoryRevision)) == 1
    assert http.delete("/memory/one?expected_revision=2").status_code == 200

def test_audit_failure_rolls_back_content_and_history(setup, monkeypatch):
    http, sessions = setup
    def fail(*args):
        raise RuntimeError("Simulated conflict/audit failure")
    monkeypatch.setattr(memory, "detect_conflicts", fail)
    assert http.patch("/memory/one", json={"text": "Should roll back", "expected_revision": 1}).status_code == 500
    with sessions() as db:
        assert db.get(Memory, "one").text == "Original"
        assert db.get(Memory, "one").revision == 1
        assert db.scalar(select(func.count()).select_from(MemoryRevision)) == 0

def test_concurrent_orm_writers_cannot_bypass_revision(setup):
    _, sessions = setup
    with sessions() as first, sessions() as second:
        a, b = first.get(Memory, "one"), second.get(Memory, "one")
        a.text = "Winner"
        first.commit()
        b.text = "Loser"
        with pytest.raises(StaleDataError):
            second.commit()

def test_legacy_patch_and_invalid_revision(setup):
    http, _ = setup
    assert http.patch("/memory/one", json={"text": "Legacy"}).status_code == 200
    for value in [0, -1, True, "1"]:
        assert http.patch("/memory/one", json={"expected_revision": value}).status_code == 422


def test_concurrent_delete_cannot_remove_newer_content(setup):
    _, sessions = setup
    with sessions() as first, sessions() as second:
        newer, stale = first.get(Memory, "one"), second.get(Memory, "one")
        newer.text = "Keep this edit"
        first.commit()
        second.delete(stale)
        with pytest.raises(StaleDataError):
            second.commit()
    with sessions() as db:
        assert db.get(Memory, "one").text == "Keep this edit"
