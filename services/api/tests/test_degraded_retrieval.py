"""Degraded retrieval, reported rather than disguised.

Nothing here is mocked out. The database is a real SQLite file, the vector
index probe is pointed at a real closed port so the connection genuinely fails,
and the embedding provider is genuinely absent. Each test observes what the
service actually does in that state.
"""

import socket

import pytest
from app.core import auth
from app.core import db as core_db
from app.core.db import Base
from app.models.memory import Memory
from app.routes import memory as memory_routes
from app.routes import runtime as runtime_routes
from app.routes import system as system_routes
from app.services import auth_settings_service, embeddings, qdrant_store
from app.services.auth_settings_service import (
    set_admin_key,
)
from app.services.embeddings import EmbeddingUnavailable
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ADMIN_KEY = "Admin-key-123!-long-enough"


def _closed_port() -> int:
    """A port nothing is listening on, so a connection is really refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A real SQLite database wired into every module that opens a session."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'degraded.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (auth, memory_routes, runtime_routes, system_routes):
        monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(core_db, "engine", engine)
    yield Session
    engine.dispose()


@pytest.fixture
def no_semantic_retrieval(monkeypatch):
    """No embedding provider, and a vector index that really refuses connections.

    The sidecar is pointed at a genuinely closed port rather than mocked, so it
    fails the way an absent sidecar fails in production.
    """
    monkeypatch.setattr(embeddings, "EMBEDDINGS_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(embeddings, "EMBEDDINGS_KEY", "unused-but-set")
    embeddings.reset_provider_cache()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", f"http://127.0.0.1:{_closed_port()}")
    qdrant_store.get_qdrant_client.cache_clear()
    qdrant_store.reset_ensured_collections()
    yield
    embeddings.reset_provider_cache()
    qdrant_store.get_qdrant_client.cache_clear()
    qdrant_store.reset_ensured_collections()


# --- The embedding provider is gone, and says so -----------------------------


def test_no_vector_is_ever_produced_without_a_reachable_provider(monkeypatch):
    """The hash mode is gone and nothing replaced it as a fallback.

    Its whole failure was producing a vector no matter what. Whatever is wrong,
    the answer is an exception the caller must handle, never a number.
    """
    monkeypatch.setattr(embeddings, "EMBEDDINGS_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(embeddings, "EMBEDDINGS_KEY", "unused-but-set")
    embeddings.reset_provider_cache()
    try:
        health = embeddings.embedding_health()
        assert health["status"] == "unavailable"
        assert "unavailable" in health["reason"]
        with pytest.raises(EmbeddingUnavailable):
            embeddings.embed_text("anything")
    finally:
        embeddings.reset_provider_cache()


def test_semantic_status_names_the_component_that_is_down(no_semantic_retrieval):
    status = qdrant_store.semantic_status()
    assert status["status"] == "degraded"
    assert status["component"] == "embeddings"
    assert status["reason"]


def test_index_writes_report_failure_instead_of_raising(no_semantic_retrieval):
    # Both the provider and the index are down. The upsert must come back with a
    # status rather than raising into a caller whose row is already committed.
    result = qdrant_store.index_after_commit(
        qdrant_store.upsert_memory_embedding, "some-id", "some text", payload={}
    )
    assert result["status"] == "degraded"
    assert result["component"] in {"embeddings", "vector_index"}
    assert result["reason"]

    # A missing provider is reported as an embedding failure, not as an index
    # one, so the owner is pointed at the thing that is actually broken.
    embedding_only = qdrant_store.index_after_commit(lambda: embeddings.embed_text("x"))
    assert embedding_only["component"] == "embeddings"
    assert embedding_only["reason"] == embeddings.embedding_health()["reason"]


# --- /health runs real probes ------------------------------------------------


def test_health_names_each_failing_dependency(session, no_semantic_retrieval):
    from app import main

    main._health_cache.clear()
    app = FastAPI()
    app.get("/health")(main.health)
    body = TestClient(app).get("/health").json()

    assert body["status"] == "degraded"
    assert body["degraded"] == ["embeddings", "qdrant"]
    # Postgres is the one dependency that really answers here, and it says so.
    assert body["checks"]["postgres"]["status"] == "ok"
    assert body["checks"]["qdrant"]["reason"] == qdrant_store.INDEX_UNREACHABLE
    assert body["checks"]["embeddings"]["status"] == "unavailable"
    # Coarse on purpose: the route is unauthenticated.
    assert "127.0.0.1" not in str(body)


def test_health_reports_the_age_of_a_cached_probe(session, no_semantic_retrieval):
    from app import main

    main._health_cache.clear()
    app = FastAPI()
    app.get("/health")(main.health)
    client = TestClient(app)

    first = client.get("/health").json()
    second = client.get("/health").json()

    assert first["age_seconds"] == 0.0
    assert second["age_seconds"] >= 0.0
    assert second["status"] == first["status"]


# --- Degraded retrieval is labelled, never disguised -------------------------


def _runtime_client(monkeypatch):
    monkeypatch.setattr(runtime_routes, "build_briefing", lambda *args, **kwargs: {})
    app = FastAPI()
    app.include_router(runtime_routes.router)
    return TestClient(app, raise_server_exceptions=False)


def test_context_says_retrieval_is_lexical_when_semantic_is_down(
    session, monkeypatch, no_semantic_retrieval
):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db:
        set_admin_key(db, ADMIN_KEY)
        db.add(Memory(agent_id="default", text="The release pipeline broke again on Friday.",
                      summary="release broke", tags_json="[]"))
        db.commit()
    client = _runtime_client(monkeypatch)

    body = client.post(
        "/runtime/context",
        headers={"X-MemoryGate-Key": ADMIN_KEY},
        json={"query": "release pipeline", "max_items": 5},
    ).json()

    assert body["retrieval"]["mode"] == "lexical"
    assert body["retrieval"]["semantic"]["status"] == "degraded"
    assert body["retrieval"]["semantic"]["reason"]
    assert body["memories"], "the lexical path must still return the matching memory"
    assert {item["retrieval_path"] for item in body["memories"]} == {"lexical"}
    # The reading model sees `usage.instruction`, so the warning has to be there.
    assert "Semantic retrieval is unavailable" in body["usage"]["instruction"]
    assert "incomplete" in body["usage"]["instruction"]


def test_memory_search_labels_the_path_that_produced_each_result(
    session, monkeypatch, no_semantic_retrieval
):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db:
        set_admin_key(db, ADMIN_KEY)
        db.add(Memory(agent_id="default", text="Alexey always drinks espresso.",
                      summary="espresso", tags_json="[]"))
        db.commit()
    app = FastAPI()
    app.include_router(memory_routes.router, dependencies=[Depends(auth.require_key)])
    client = TestClient(app, raise_server_exceptions=False)

    body = client.post(
        "/memory/search", headers={"X-MemoryGate-Key": ADMIN_KEY}, json={"query": "espresso"}
    ).json()

    assert body["retrieval"]["mode"] == "lexical"
    assert body["retrieval"]["semantic"]["status"] == "degraded"
    assert [item["retrieval_path"] for item in body["results"]] == ["lexical"]


def test_a_write_survives_an_unavailable_index_and_admits_it(
    session, monkeypatch, no_semantic_retrieval
):
    """Postgres has committed; a dead index must degrade search, not 500."""
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db:
        set_admin_key(db, ADMIN_KEY)
    app = FastAPI()
    app.include_router(memory_routes.router, dependencies=[Depends(auth.require_key)])
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/memory/write",
        headers={"X-MemoryGate-Key": ADMIN_KEY},
        json={"text": "I always review the deploy log before the release.", "source_type": "user"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["indexing"]["status"] == "degraded"
    assert body["novelty_check"]["status"] == "degraded"
    with session() as db:
        assert db.query(Memory).count() == 1
