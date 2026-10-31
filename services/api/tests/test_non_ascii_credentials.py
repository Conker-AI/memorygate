"""A credential or confirmation outside ASCII is refused like any wrong one, never a 500.

`compare_digest` raises TypeError on a str that is not ASCII, and Starlette decodes header
values as latin-1, so a header such as "clé" used to escape the key check as an unhandled
exception. Every check now compares bytes. The clients keep raise_server_exceptions on, so a
TypeError fails the test instead of hiding behind a 500.
"""

import json

import pytest
from app.core import auth
from app.core.db import Base
from app.models.evidence_source import EvidenceSource
from app.routes import conversation, runtime, system
from app.routes import memory as memory_routes
from app.services import auth_settings_service, backup_service
from app.services.auth_settings_service import set_admin_key
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ADMIN_KEY = "Admin-key-123!-long-enough"
NON_ASCII = "clé-not-the-key".encode("latin-1")


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'keys.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (auth, memory_routes, runtime, system):
        monkeypatch.setattr(module, "SessionLocal", Session)
    # Failed attempts are counted per client host for the whole process; start from none.
    monkeypatch.setattr(auth_settings_service, "_attempt_state", {})
    yield Session
    engine.dispose()


def client_for(router, *dependencies):
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(check) for check in dependencies])
    return TestClient(app)


def test_environment_admin_key_refuses_a_non_ascii_header(session, monkeypatch):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", ADMIN_KEY)
    client = client_for(memory_routes.router, auth.require_key)

    assert client.get("/memory", headers={"X-MemoryGate-Key": NON_ASCII}).status_code == 401
    assert client.get("/memory", headers={"X-MemoryGate-Key": ADMIN_KEY}).status_code == 200


def test_conversation_key_refuses_a_non_ascii_header(monkeypatch):
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_KEY", "conversation-test-key-long")
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_AGENT_ID", "owner")
    client = client_for(conversation.router)

    response = client.put(
        "/runtime/conversation/msg_keys",
        json={"session_id": "ses_keys", "content": "hello", "created_at": 1770000000.0},
        headers={"X-MemoryGate-Conversation-Key": NON_ASCII},
    )

    assert response.status_code == 401


def test_listener_key_refuses_a_non_ascii_header(session):
    with session() as db:
        db.add(
            EvidenceSource(
                source_key="phone",
                source_type="listener",
                label="Phone",
                secret_json=json.dumps({"ingest_key": "listener-test-key-long"}),
            )
        )
        db.commit()
    client = client_for(runtime.router)

    response = client.post(
        "/runtime/listeners/phone",
        json={"content": "hello"},
        headers={"X-MemoryGate-Listener-Key": NON_ASCII},
    )

    assert response.status_code == 401


def test_reset_confirmation_refuses_non_ascii_text(session, monkeypatch, tmp_path):
    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", "")
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path / "backups"))
    with session() as db:
        set_admin_key(db, ADMIN_KEY)
    client = client_for(system.router, auth.require_key)

    response = client.post(
        "/system/memory-reset",
        headers={"X-MemoryGate-Key": ADMIN_KEY},
        json={"current_key": ADMIN_KEY, "confirmation": "RÉSET MEMORY"},
    )

    assert response.status_code == 400
