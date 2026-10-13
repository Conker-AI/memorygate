from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from app.core.db import Base
from app.models.memory import Memory
from app.models.vector_outbox import VectorOutbox
from app.services import vector_outbox
from app.services.embeddings import EmbeddingUnavailable
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "outbox.db"
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    yield path, engine, sessions
    engine.dispose()


def enqueue_memory(db, identity="memory-1", text_value="Current text"):
    db.add(Memory(id=identity, agent_id="owner", text=text_value))
    db.flush()
    return vector_outbox.enqueue_upsert(
        db,
        "memory",
        identity,
        text_value,
        agent_id="owner",
        payload={"agent_id": "owner", "memory_type": "context"},
    )


def test_intent_rolls_back_with_authoritative_write(database):
    _, _, sessions = database
    with sessions() as db:
        enqueue_memory(db)
        db.rollback()

    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 0
        assert db.scalar(select(func.count()).select_from(VectorOutbox)) == 0


def test_failure_stays_visible_and_retry_converges(database, monkeypatch):
    _, _, sessions = database
    with sessions() as db:
        enqueue_memory(db)
        db.commit()

    def unavailable(*args, **kwargs):
        raise ConnectionError("qdrant unavailable")

    monkeypatch.setattr(vector_outbox.qdrant_store, "upsert_memory_embedding", unavailable)
    result = vector_outbox.reconcile_one("memory", "memory-1", sessions=sessions)
    assert result == {
        "status": "degraded",
        "component": "vector_index",
        "reason": "vector index unreachable",
    }
    with sessions() as db:
        row = db.scalar(select(VectorOutbox))
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.last_error == "ConnectionError: vector index unreachable"
        row.next_attempt_at = datetime.now(UTC)
        db.commit()

    indexed = {}

    def upsert(identity, text_value, payload):
        indexed[identity] = (text_value, payload)

    monkeypatch.setattr(vector_outbox.qdrant_store, "upsert_memory_embedding", upsert)
    assert vector_outbox.reconcile_pending(sessions=sessions) == 1
    assert indexed == {
        "memory-1": (
            "Current text",
            {"agent_id": "owner", "memory_type": "context"},
        )
    }
    with sessions() as db:
        row = db.scalar(select(VectorOutbox))
        assert row.status == "complete" and row.last_error == ""


def test_embedding_failure_keeps_the_specific_component(database, monkeypatch):
    _, _, sessions = database
    with sessions() as db:
        enqueue_memory(db)
        db.commit()

    def unavailable(*args, **kwargs):
        raise EmbeddingUnavailable("embedding provider unavailable")

    monkeypatch.setattr(vector_outbox.qdrant_store, "upsert_memory_embedding", unavailable)
    result = vector_outbox.reconcile_one("memory", "memory-1", sessions=sessions)
    assert result == {
        "status": "degraded",
        "component": "embeddings",
        "reason": "embedding provider unavailable",
    }


def test_delete_supersedes_an_unapplied_upsert(database, monkeypatch):
    _, _, sessions = database
    with sessions() as db:
        assert enqueue_memory(db) == 1
        assert (
            vector_outbox.enqueue_delete(
                db, "memory", "memory-1", agent_id="owner"
            )
            == 2
        )
        db.delete(db.get(Memory, "memory-1"))
        db.commit()

    calls = []
    monkeypatch.setattr(
        vector_outbox.qdrant_store,
        "upsert_memory_embedding",
        lambda *args, **kwargs: calls.append("upsert"),
    )
    monkeypatch.setattr(
        vector_outbox.qdrant_store,
        "delete_memory_embedding",
        lambda identity: calls.append(("delete", identity)),
    )
    assert vector_outbox.reconcile_pending(sessions=sessions) == 1
    assert calls == [("delete", "memory-1")]


def test_concurrent_workers_apply_one_generation_once(database, monkeypatch):
    _, _, sessions = database
    with sessions() as db:
        enqueue_memory(db)
        db.commit()

    calls = []
    monkeypatch.setattr(
        vector_outbox.qdrant_store,
        "upsert_memory_embedding",
        lambda identity, text_value, payload: calls.append(identity),
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: vector_outbox.reconcile_pending(1, sessions=sessions),
                range(4),
            )
        )
    assert sum(results) == 1
    assert calls == ["memory-1"]


def test_restart_replays_effect_without_creating_a_second_point(database, monkeypatch):
    path, engine, sessions = database
    with sessions() as db:
        enqueue_memory(db)
        db.commit()

    points = {}

    def upsert(identity, text_value, payload):
        points[identity] = {"text": text_value, "payload": payload}

    monkeypatch.setattr(vector_outbox.qdrant_store, "upsert_memory_embedding", upsert)

    class CommitCrashSession(Session):
        crash_once = True

        def commit(self):
            if self.crash_once:
                type(self).crash_once = False
                raise RuntimeError("process died before outbox acknowledgement")
            return super().commit()

    crashing_sessions = sessionmaker(
        engine, class_=CommitCrashSession, expire_on_commit=False, autoflush=False
    )
    with pytest.raises(RuntimeError, match="before outbox acknowledgement"):
        vector_outbox.reconcile_pending(1, sessions=crashing_sessions)
    assert list(points) == ["memory-1"]

    engine.dispose()
    restarted_engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    restarted_sessions = sessionmaker(
        restarted_engine, expire_on_commit=False, autoflush=False
    )
    try:
        assert vector_outbox.reconcile_pending(1, sessions=restarted_sessions) == 1
        assert list(points) == ["memory-1"]
        with restarted_sessions() as db:
            assert db.scalar(select(VectorOutbox.status)) == "complete"
    finally:
        restarted_engine.dispose()
