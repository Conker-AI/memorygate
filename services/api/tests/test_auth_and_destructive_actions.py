"""Auth that fails closed, and destructive actions that need more than a key.

Nothing here is mocked: the database is a real SQLite file and the CORS policy
is exercised through a real preflight request.
"""

import socket

import pytest
from app.core import auth, config
from app.core import db as core_db
from app.core.db import Base
from app.models.memory import Memory
from app.routes import memory as memory_routes
from app.routes import runtime as runtime_routes
from app.routes import system as system_routes
from app.services import auth_settings_service, backup_service, embeddings, qdrant_store
from app.services.auth_settings_service import (
    MIN_ENV_ADMIN_KEY_LENGTH,
    assert_admin_key_configured,
    set_admin_key,
    verify_admin_key,
)
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
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


# --- Secure by default, or refuse to start -----------------------------------


def test_startup_refuses_when_no_admin_key_is_configured(session, monkeypatch):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db, pytest.raises(RuntimeError) as raised:
        assert_admin_key_configured(db)
    message = str(raised.value)
    assert "refuses to start" in message
    # The error has to name the exact fix, not just the problem.
    assert "MEMORYGATE_ADMIN_KEY" in message
    assert ".env" in message
    assert str(MIN_ENV_ADMIN_KEY_LENGTH) in message


def test_startup_refuses_a_too_short_environment_key(session, monkeypatch):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "short")
    with session() as db, pytest.raises(RuntimeError) as raised:
        assert_admin_key_configured(db)
    assert "too short" in str(raised.value)
    assert "MEMORYGATE_ADMIN_KEY" in str(raised.value)


def test_startup_accepts_either_key_source(session, monkeypatch):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "x" * MIN_ENV_ADMIN_KEY_LENGTH)
    with session() as db:
        assert assert_admin_key_configured(db) == "environment"
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db:
        set_admin_key(db, ADMIN_KEY)
        assert assert_admin_key_configured(db) == "database"


def test_an_unconfigured_instance_authenticates_nobody(session, monkeypatch):
    """This used to return True, which made a fresh instance fully open."""
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db:
        assert verify_admin_key(db, None) is False
        assert verify_admin_key(db, "anything") is False


def test_admin_routes_reject_a_request_with_no_key(session, monkeypatch):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db:
        set_admin_key(db, ADMIN_KEY)
    app = FastAPI()
    app.include_router(memory_routes.router, dependencies=[Depends(auth.require_key)])
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/memory").status_code == 401
    assert client.get("/memory", headers={"X-MemoryGate-Key": "wrong"}).status_code == 401
    assert client.get("/memory", headers={"X-MemoryGate-Key": ADMIN_KEY}).status_code == 200


def test_cors_does_not_default_to_any_origin():
    assert config.CORS_ANY_ORIGIN not in config.CORS_ORIGINS
    assert config.CORS_ORIGINS == ["http://localhost:8021", "http://127.0.0.1:8021"]


def test_a_browser_origin_that_is_not_allowed_gets_no_cors_grant():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/probe")
    def probe():
        return {"status": "ok"}

    client = TestClient(app)
    denied = client.options(
        "/probe",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    allowed = client.options(
        "/probe",
        headers={"Origin": "http://localhost:8021", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in denied.headers
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:8021"


# --- A destructive action needs more than admin auth -------------------------


def _system_client(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path / "backups"))
    app = FastAPI()
    app.include_router(system_routes.router, dependencies=[Depends(auth.require_key)])
    return TestClient(app, raise_server_exceptions=False)


def test_memory_reset_needs_the_typed_phrase_as_well_as_the_admin_key(
    session, monkeypatch, tmp_path, no_semantic_retrieval
):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    with session() as db:
        set_admin_key(db, ADMIN_KEY)
        db.add(Memory(agent_id="default", text="Owner prefers local-first tools.", summary="local first", tags_json="[]"))
        db.commit()
    client = _system_client(monkeypatch, tmp_path)
    headers = {"X-MemoryGate-Key": ADMIN_KEY}

    without_key = client.post(
        "/system/memory-reset", json={"current_key": ADMIN_KEY, "confirmation": system_routes.MEMORY_RESET_PHRASE}
    )
    wrong_phrase = client.post(
        "/system/memory-reset", headers=headers, json={"current_key": ADMIN_KEY, "confirmation": "reset memory"}
    )
    accepted = client.post(
        "/system/memory-reset",
        headers=headers,
        json={"current_key": ADMIN_KEY, "confirmation": system_routes.MEMORY_RESET_PHRASE},
    )

    assert without_key.status_code == 401
    # A valid admin key alone must not be enough to wipe the workspace.
    assert wrong_phrase.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json()["removed"]["memories"] == 1
    with session() as db:
        assert db.query(Memory).count() == 0
