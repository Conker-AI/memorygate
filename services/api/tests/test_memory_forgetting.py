"""Forgetting removes every stored copy of a memory's text. Temporary SQLite; no network."""

import json

import pytest
from app.core.db import Base
from app.models.audit import MemoryAudit
from app.models.deletion_receipt import DeletionReceipt
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_forget import MemoryForget
from app.models.memory_revision import MemoryRevision
from app.routes import corrections
from app.routes import memory as memory_routes
from app.services import memory_corrections, memory_forgetting
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

KEY = "synthetic-correction-key-for-tests-12345"
HEADERS = {"X-MemoryGate-Correction-Key": KEY}
SECRET = "Owner lives at 12 Secret Street"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'forget.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_KEY", KEY)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_AGENT_ID", "owner")
    for module in (memory_corrections, memory_forgetting, memory_routes):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    removed = []
    monkeypatch.setattr(memory_corrections, "index_after_commit", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(memory_forgetting, "delete_memory_embedding", removed.append)
    monkeypatch.setattr(memory_routes, "delete_memory_embedding", removed.append)
    with sessions() as db:
        for identity, agent, value in (("one", "owner", SECRET), ("two", "owner", "Likes tea"), ("foreign", "other", SECRET)):
            db.add(Memory(id=identity, agent_id=agent, text=value, summary=value, source_type="stated", confidence="high"))
        db.add(MemoryAudit(action="write", memory_id="one", payload_json=json.dumps({"text": SECRET})))
        db.add(MemoryConflict(agent_id="owner", memory_id="two", conflicting_memory_id="one", reason=f"Conflicts with {SECRET}"))
        db.commit()
    app = FastAPI()
    app.include_router(corrections.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, sessions, engine, removed
    engine.dispose()


def forget(http, request_id="forget_request_0001", **changes):
    return http.put(
        "/runtime/corrections/forget/" + request_id,
        headers=HEADERS,
        json={"memory_id": "one", "expected_revision": 2, **changes},
    )


def texts_everywhere(engine):
    """Every text value in every table, to prove the secret survives nowhere."""
    found = []
    with engine.connect() as db:
        for table in inspect(engine).get_table_names():
            for row in db.execute(text(f'SELECT * FROM "{table}"')):
                found.extend(str(value) for value in row)
    return " ".join(found)


def test_forget_removes_the_memory_and_every_copy_of_its_text(setup):
    http, sessions, engine, removed = setup
    # A correction stores the old text in a revision snapshot; forgetting must remove it.
    corrected = http.put(
        "/runtime/corrections/correction_request_01",
        headers=HEADERS,
        json={"memory_id": "one", "expected_revision": 1, "text": SECRET + " (corrected)"},
    )
    assert corrected.status_code == 200
    assert SECRET in texts_everywhere(engine)

    response = forget(http)
    assert response.status_code == 200
    expected = {"request_id": "forget_request_0001", "memory_id": "one", "agent_id": "owner", "revision": 2, "status": "forgotten", "index_removal": "removed"}
    assert response.json() == expected
    assert removed == ["one"]
    # Only the other agent's unrelated memory still holds that sentence.
    with sessions() as db:
        db.delete(db.get(Memory, "foreign"))
        db.commit()
    assert SECRET not in texts_everywhere(engine)
    with sessions() as db:
        assert db.get(Memory, "one") is None
        assert db.get(Memory, "two") is not None
        assert not db.scalars(select(MemoryRevision).where(MemoryRevision.memory_id == "one")).all()
        assert not db.scalars(select(MemoryConflict)).all()
        assert db.get(DeletionReceipt, ("owner", "memory", "one")) is not None
        actions = [(row.action, json.loads(row.payload_json)) for row in db.scalars(select(MemoryAudit).where(MemoryAudit.memory_id == "one"))]
        assert ("owner_forget", {"request_id": "forget_request_0001", "revision": 2}) in actions
        assert all("text" not in payload for _, payload in actions)

    # Replays return the saved receipt; a different request for the gone memory is 404.
    assert forget(http).json() == expected
    assert http.get("/runtime/corrections/forget/forget_request_0001", headers=HEADERS).json() == expected
    assert forget(http, request_id="forget_request_0002").status_code == 404
    assert forget(http, expected_revision=3).status_code == 409
    assert removed == ["one"]


def test_forget_is_bounded_by_revision_namespace_and_capability(setup):
    http, sessions, _, removed = setup
    assert forget(http, expected_revision=1, memory_id="two").status_code == 200
    assert forget(http, request_id="forget_request_0003", memory_id="one", expected_revision=5).status_code == 409
    assert forget(http, request_id="forget_request_0004", memory_id="foreign", expected_revision=1).status_code == 404
    assert http.put("/runtime/corrections/forget/forget_request_0005", json={"memory_id": "one", "expected_revision": 1}).status_code == 401
    wrong = http.put("/runtime/corrections/forget/forget_request_0006", headers={**HEADERS, "X-Agent-Id": "other"}, json={"memory_id": "one", "expected_revision": 1})
    assert wrong.status_code == 403
    assert http.put("/runtime/corrections/forget/forget_request_0007", headers=HEADERS, json={"memory_id": "one", "expected_revision": 1, "text": "extra"}).status_code == 422
    with sessions() as db:
        assert db.get(Memory, "one") is not None and db.get(Memory, "foreign") is not None
        assert db.get(MemoryForget, ("owner", "forget_request_0003")) is None
    assert removed == ["two"]


def test_index_outage_keeps_the_forget_and_says_so(setup, monkeypatch):
    http, sessions, _, _ = setup

    def unreachable(memory_id):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(memory_forgetting, "delete_memory_embedding", unreachable)
    response = http.put("/runtime/corrections/forget/forget_request_0008", headers=HEADERS, json={"memory_id": "two", "expected_revision": 1})
    assert response.status_code == 200 and response.json()["index_removal"] == "degraded"
    with sessions() as db:
        assert db.get(Memory, "two") is None


def test_admin_delete_no_longer_copies_text_into_the_audit(setup):
    _, sessions, engine, _ = setup
    memory_routes.delete_memory("one", agent_id="owner", expected_revision=None)
    with sessions() as db:
        db.delete(db.get(Memory, "foreign"))
        db.commit()
    assert SECRET not in texts_everywhere(engine)
