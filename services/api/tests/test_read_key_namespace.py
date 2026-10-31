"""A read key for one agent cannot read another agent's memories, by header or by body."""

import pytest
from app.core import auth
from app.core.db import Base
from app.models.memory import Memory
from app.routes import runtime
from app.services import auth_settings_service as keys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RAW = "mg_read_synthetic_specialist_namespace_key"


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    monkeypatch.setattr(auth, "SessionLocal", sessions)
    monkeypatch.setattr(runtime, "SessionLocal", sessions)
    monkeypatch.setattr(keys, "MEMORYGATE_ADMIN_KEY", "synthetic-unused-admin-key")
    monkeypatch.setattr(keys, "_attempt_state", {})
    with sessions() as db:
        keys.ensure_bootstrap_agent_access_key(db, RAW, "specialist")
        db.add_all(
            [
                Memory(id="own", agent_id="specialist", text="Own memory", status="active"),
                Memory(id="foreign", agent_id="companion", text="Foreign memory", status="active"),
            ]
        )
        db.commit()
    app = FastAPI()
    app.include_router(runtime.router)
    with TestClient(app) as client:
        yield client
    engine.dispose()


def context(client, header_agent, body):
    return client.post(
        "/runtime/context",
        headers={"X-MemoryGate-Key": RAW, "X-Agent-Id": header_agent},
        json={"query": "memory", "scope": "selected", "memory_ids": ["own", "foreign"], **body},
    )


def test_body_cannot_switch_the_namespace(api):
    response = context(api, "specialist", {"agent_id": "companion"})
    assert response.status_code == 403
    assert "Foreign" not in response.text


def test_header_for_another_agent_is_not_this_key(api):
    response = context(api, "companion", {})
    assert response.status_code == 401
    assert "Foreign" not in response.text


def test_own_namespace_returns_only_own_memories(api):
    response = context(api, "specialist", {"agent_id": "specialist"})
    assert response.status_code == 200, response.text
    assert "Own memory" in response.text and "Foreign memory" not in response.text


def test_a_new_agent_id_per_try_earns_no_fresh_attempts(api):
    """X-Agent-Id is the caller's text; unknown ids share the host's lockout bucket."""
    for attempt in range(4):
        wrong = api.post(
            "/runtime/context",
            headers={"X-MemoryGate-Key": "wrong", "X-Agent-Id": f"made-up-{attempt}"},
            json={"query": "memory"},
        )
        assert wrong.status_code == 401
    fifth = api.post(
        "/runtime/context",
        headers={"X-MemoryGate-Key": "wrong", "X-Agent-Id": "made-up-4"},
        json={"query": "memory"},
    )
    assert fifth.status_code == 429
    assert len(keys._attempt_state) == 1
    # The agent that does hold a key keeps its own bucket.
    assert context(api, "specialist", {}).status_code == 200
