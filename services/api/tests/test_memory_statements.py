"""Owner-approved statements over the correction capability; real temporary SQL, no network."""

import pytest
from app.core import auth
from app.core.db import Base
from app.models.audit import MemoryAudit
from app.models.memory import Memory
from app.models.memory_revision import MemoryRevision
from app.routes import corrections
from app.services import auth_settings_service, memory_corrections
from app.services import memory_statements as service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

KEY = "synthetic-correction-key-for-tests-12345"
HEADERS = {"X-MemoryGate-Correction-Key": KEY}
REQUEST = "creq_" + "a" * 32
FACT = "The physics exam is on Thursday 30 October."


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'statements.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_KEY", KEY)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_AGENT_ID", "owner")
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(memory_corrections, "SessionLocal", sessions)
    monkeypatch.setattr(auth, "SessionLocal", sessions)
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "separate-synthetic-admin")
    calls = []
    monkeypatch.setattr(
        service, "_reconcile_index_intent", lambda *a, **k: calls.append(a) or {"status": "ok"}
    )
    app = FastAPI()
    app.include_router(corrections.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, sessions, calls
    engine.dispose()


def put(http, request_id=REQUEST, headers=HEADERS, **body):
    return http.put(
        "/runtime/corrections/remember/" + request_id,
        headers=headers,
        json=body or {"text": FACT},
    )


def test_stores_the_exact_text_once_with_a_receipt(setup):
    http, sessions, calls = setup
    first = put(http)
    assert first.status_code == 200
    receipt = first.json()
    assert receipt["status"] == "remembered" and receipt["indexing"] == "indexed"
    assert put(http).json() == receipt  # a replay stores nothing new
    assert len(calls) == 1
    with sessions() as db:
        [memory] = db.execute(select(Memory)).scalars().all()
        assert (memory.id, memory.agent_id, memory.text, memory.summary) == (
            receipt["memory_id"],
            "owner",
            FACT,
            FACT,
        )
        assert (memory.source_type, memory.confidence) == ("user", "high")
        assert db.scalar(select(func.count()).select_from(MemoryRevision)) == 1
        [audit] = db.execute(select(MemoryAudit)).scalars().all()
        assert audit.action == "owner_stated_memory" and FACT not in audit.payload_json
    reconciled = http.get("/runtime/corrections/" + REQUEST, headers=HEADERS)
    assert reconciled.status_code == 200 and reconciled.json()["memory_id"] == receipt["memory_id"]


def test_the_same_fact_is_not_stored_twice(setup):
    http, sessions, _ = setup
    first = put(http).json()
    again = put(
        http, request_id="creq_" + "b" * 32, text="  the physics exam is on thursday 30 october."
    )
    assert again.status_code == 200
    assert again.json()["status"] == "already_remembered"
    assert again.json()["memory_id"] == first["memory_id"]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 1


def test_reusing_a_request_for_other_text_is_refused(setup):
    http, sessions, _ = setup
    put(http)
    assert put(http, text="Something else entirely.").status_code == 409
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "a\x00b"},
        {"text": "x" * 2001},
        {"text": FACT, "extra": 1},
    ],
)
def test_invalid_statements_store_nothing(setup, body):
    http, sessions, _ = setup
    response = put(http, **body)
    assert response.status_code == 422 and FACT not in response.text
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 0


def test_needs_the_correction_capability(setup):
    http, sessions, _ = setup
    assert put(http, headers={}).status_code == 401
    assert (
        put(http, headers={"X-MemoryGate-Correction-Key": "separate-synthetic-admin"}).status_code
        == 401
    )
    assert put(http, headers={**HEADERS, "X-Agent-Id": "other"}).status_code == 403
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 0
