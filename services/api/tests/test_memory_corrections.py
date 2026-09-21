"""Real temporary SQL transactions and HTTP capability boundaries; no network calls."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from app.core import auth
from app.core.db import Base
from app.models.audit import MemoryAudit
from app.models.memory import Memory
from app.models.memory_correction import MemoryCorrection
from app.models.memory_revision import MemoryRevision
from app.routes import corrections
from app.services import auth_settings_service
from app.services import memory_corrections as service
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

KEY = "synthetic-correction-key-for-tests-12345"
HEADERS = {"X-MemoryGate-Correction-Key": KEY}
REQUEST = "correction_request_0001"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "corrections.db"
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_KEY", KEY)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_AGENT_ID", "owner")
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(auth, "SessionLocal", sessions)
    monkeypatch.setattr(
        auth_settings_service, "MEMORYGATE_ADMIN_KEY", "separate-synthetic-admin"
    )
    calls = []

    def index(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "ok"}

    monkeypatch.setattr(service, "index_after_commit", index)
    with sessions() as db:
        db.add_all(
            [
                Memory(
                    id=identity,
                    agent_id=agent,
                    text="Original",
                    summary="Old summary",
                    source_type="inferred",
                    confidence="low",
                )
                for identity, agent in (
                    ("one", "owner"),
                    ("two", "owner"),
                    ("foreign", "other"),
                )
            ]
        )
        db.commit()
    app = FastAPI()
    app.include_router(corrections.router)

    @app.get("/admin-only", dependencies=[Depends(auth.require_key)])
    def admin():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, sessions, calls, path
    engine.dispose()


def put(http, request_id=REQUEST, **changes):
    return http.put(
        "/runtime/corrections/" + request_id,
        headers=HEADERS,
        json={
            "memory_id": "one",
            "expected_revision": 1,
            "text": "Corrected",
            **changes,
        },
    )


def test_atomic_edit_preserves_metadata_and_exact_summary(setup):
    http, sessions, calls, _ = setup
    baseline = http.get("/runtime/corrections/memories/one", headers=HEADERS).json()
    assert baseline == {
        "id": "one",
        "agent_id": "owner",
        "revision": 1,
        "text": "Original",
        "source_type": "inferred",
        "confidence": "low",
    }
    response = put(http)
    assert response.status_code == 200
    expected = {
        "request_id": REQUEST,
        "memory_id": "one",
        "agent_id": "owner",
        "previous_revision": 1,
        "revision": 2,
        "status": "applied",
        "indexing": "indexed",
    }
    assert response.json() == expected
    assert put(http).json() == expected
    assert (
        http.get("/runtime/corrections/" + REQUEST, headers=HEADERS).json() == expected
    )
    assert len(calls) == 1
    with sessions() as db:
        row = db.get(Memory, "one")
        assert row.text == row.summary == "Corrected"
        assert (row.source_type, row.confidence, row.memory_type, row.status) == (
            "inferred",
            "low",
            "context",
            "active",
        )
        assert db.scalar(select(func.count()).select_from(MemoryRevision)) == 1
        assert db.scalar(select(func.count()).select_from(MemoryAudit)) == 1


def test_stale_and_conflicting_requests_do_not_mutate(setup):
    http, sessions, calls, _ = setup
    assert put(http).status_code == 200
    assert put(http, text="Different").status_code == 409
    assert put(http, request_id="another_request_0002").status_code == 409
    assert put(http, memory_id="two").status_code == 409
    assert len(calls) == 1
    with sessions() as db:
        assert db.get(Memory, "one").revision == 2
        assert db.get(Memory, "two").revision == 1
        assert db.scalar(select(func.count()).select_from(MemoryCorrection)) == 1


def test_capability_namespace_and_admin_are_separate(setup):
    http, _, _, _ = setup
    url = "/runtime/corrections/memories/one"
    assert http.get(url).status_code == 401
    assert (
        http.get(
            url, headers={"X-MemoryGate-Key": "separate-synthetic-admin"}
        ).status_code
        == 401
    )
    assert (
        http.get(
            url, headers={"X-MemoryGate-Correction-Key": "separate-synthetic-admin"}
        ).status_code
        == 401
    )
    assert http.get(url, headers={**HEADERS, "X-Agent-Id": "other"}).status_code == 403
    assert http.get("/admin-only", headers={"X-MemoryGate-Key": KEY}).status_code == 401
    assert (
        http.get("/runtime/corrections/memories/foreign", headers=HEADERS).status_code
        == 404
    )
    assert put(http, memory_id="foreign").status_code == 404
    assert put(http, memory_id="missing").status_code == 404
    assert (
        http.get(
            "/runtime/corrections/missing_request_0000", headers=HEADERS
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "key,agent", [("", "owner"), ("short", "owner"), (KEY, ""), (KEY, "bad/name")]
)
def test_missing_configuration_fails_closed(setup, monkeypatch, key, agent):
    http, _, _, _ = setup
    monkeypatch.setenv("MEMORYGATE_CORRECTION_KEY", key)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_AGENT_ID", agent)
    assert (
        http.get("/runtime/corrections/memories/one", headers=HEADERS).status_code
        == 401
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"text": " "},
        {"text": "x" * 16001},
        {"text": "a\x00b"},
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"expected_revision": 0},
        {"memory_id": "bad/id"},
        {"agent_id": "other"},
        {"confidence": "high"},
    ],
)
def test_validation_is_bounded_and_never_reflects_payload(setup, changes):
    http, sessions, calls, _ = setup
    response = put(http, **changes)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid correction request"}
    with sessions() as db:
        assert db.get(Memory, "one").revision == 1
    assert calls == []


def test_raw_body_bound(setup):
    http, _, _, _ = setup
    response = http.put(
        "/runtime/corrections/" + REQUEST, headers=HEADERS, content=b"x" * 128001
    )
    assert response.status_code == 413


def test_audit_failure_rolls_back_entire_transaction(setup, monkeypatch):
    http, sessions, calls, _ = setup

    def fail(*args):
        raise SQLAlchemyError("sensitive database failure never reflected")

    monkeypatch.setattr(service, "add_revision", fail)
    response = put(http)
    assert response.status_code == 503 and "sensitive" not in response.text
    with sessions() as db:
        assert db.get(Memory, "one").text == "Original"
        assert db.get(Memory, "one").revision == 1
        assert db.scalar(select(func.count()).select_from(MemoryCorrection)) == 0
        assert db.scalar(select(func.count()).select_from(MemoryAudit)) == 0
    assert calls == []


def test_concurrent_identical_and_stale_requests(setup):
    http, sessions, calls, _ = setup
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: put(http).status_code, range(4)))
    assert results == [200] * 4 and len(calls) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda i: (
                    put(
                        http,
                        request_id=f"race_request_000{i}",
                        expected_revision=2,
                        text=f"Writer {i}",
                    ).status_code
                ),
                range(2),
            )
        )
    assert sorted(results) == [200, 409]
    with sessions() as db:
        assert db.get(Memory, "one").revision == 3
        assert db.scalar(select(func.count()).select_from(MemoryCorrection)) == 2


def test_committed_timeout_replay_and_reopen_never_repeat_effect(setup, monkeypatch):
    http, sessions, _, path = setup

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(service, "index_after_commit", crash)
    payload = {"memory_id": "one", "expected_revision": 1, "text": "Corrected"}
    with pytest.raises(Crash):
        service.apply("owner", REQUEST, payload)
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(service, "SessionLocal", sessionmaker(engine, autoflush=False))
    try:
        result = put(http)
        assert result.status_code == 200 and result.json()["indexing"] == "pending"
        assert (
            http.get("/runtime/corrections/" + REQUEST, headers=HEADERS).json()
            == result.json()
        )
        with sessions() as db:
            assert db.get(Memory, "one").revision == 2
            assert db.scalar(select(func.count()).select_from(MemoryAudit)) == 1
    finally:
        engine.dispose()


def test_index_failure_is_visible_without_reapplying_text(setup, monkeypatch):
    http, _, _, _ = setup
    monkeypatch.setattr(
        service, "index_after_commit", lambda *a, **kw: {"status": "degraded"}
    )
    result = put(http)
    assert result.status_code == 200 and result.json()["indexing"] == "degraded"
    assert put(http).json() == result.json()


def test_receipt_insert_failure_rolls_back_the_edit_and_audit(setup):
    http, sessions, calls, _ = setup

    def fail(*args):
        raise SQLAlchemyError("Synthetic receipt storage failure")

    event.listen(MemoryCorrection, "before_insert", fail)
    try:
        assert put(http).status_code == 503
    finally:
        event.remove(MemoryCorrection, "before_insert", fail)
    with sessions() as db:
        assert db.get(Memory, "one").revision == 1
        assert db.get(Memory, "one").text == "Original"
        assert db.scalar(select(func.count()).select_from(MemoryRevision)) == 0
        assert db.scalar(select(func.count()).select_from(MemoryAudit)) == 0
        assert db.scalar(select(func.count()).select_from(MemoryCorrection)) == 0
    assert calls == []


def test_request_race_across_targets_rolls_back_losing_target(setup):
    http, sessions, calls, _ = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda memory: put(http, memory_id=memory).status_code, ("one", "two")
            )
        )
    assert sorted(results) == [200, 409]
    with sessions() as db:
        assert sorted(db.get(Memory, i).revision for i in ("one", "two")) == [1, 2]
        assert db.scalar(select(func.count()).select_from(MemoryCorrection)) == 1
    assert len(calls) == 1


def test_receipt_read_is_namespace_bound(setup, monkeypatch):
    http, _, _, _ = setup
    assert put(http).status_code == 200
    monkeypatch.setenv("MEMORYGATE_CORRECTION_AGENT_ID", "other")
    assert (
        http.get("/runtime/corrections/" + REQUEST, headers=HEADERS).status_code == 404
    )
