from app.core import auth
from app.core.db import Base
from app.models.memory import Memory
from app.routes import audit, runtime, transcript
from app.services.auth_settings_service import create_agent_access_key, set_admin_key
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def make_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit-metrics.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(auth, "SessionLocal", Session)
    monkeypatch.setattr(audit, "SessionLocal", Session)
    monkeypatch.setattr(runtime, "SessionLocal", Session)
    monkeypatch.setattr(transcript, "SessionLocal", Session)
    monkeypatch.setattr(runtime, "search_memory_embeddings", lambda *args, **kwargs: [])
    monkeypatch.setattr(runtime, "build_briefing", lambda *args, **kwargs: {})
    with Session() as db:
        set_admin_key(db, "Admin-key-123!")
        _, read_key = create_agent_access_key(db, "test", "hermes")
        db.add(Memory(agent_id="hermes", text="Owner prefers safe local metadata.", summary="safe metadata", tags_json="[]"))
        db.commit()
    app = FastAPI()
    app.include_router(runtime.router)
    app.include_router(transcript.router, dependencies=[Depends(auth.require_key)])
    app.include_router(audit.router, dependencies=[Depends(auth.require_key)])
    return TestClient(app), read_key


def test_audit_metrics_count_reads_and_writes_without_content(tmp_path, monkeypatch):
    client, read_key = make_client(tmp_path, monkeypatch)
    read = client.post(
        "/runtime/context",
        headers={"X-Agent-Id": "hermes", "X-MemoryGate-Key": read_key},
        json={"query": "private query token=abc123", "max_items": 3},
    )
    write = client.post(
        "/transcripts",
        headers={"X-Agent-Id": "hermes", "X-MemoryGate-Key": "Admin-key-123!"},
        json={"agent_id": "hermes", "session_id": "sess-1", "transcript": "private transcript secret=abc123"},
    )
    metrics = client.get("/audit/metrics", headers={"X-MemoryGate-Key": "Admin-key-123!"})
    audit_rows = client.get("/audit", headers={"X-MemoryGate-Key": "Admin-key-123!"})

    assert read.status_code == 200
    assert write.status_code == 200
    assert metrics.status_code == 200
    assert metrics.json()["reads"] == 1
    assert metrics.json()["writes"] == 1
    assert metrics.json()["by_action"]["runtime_context"] == 1
    assert metrics.json()["by_action"]["transcript_write"] == 1
    serialized_audit = str(audit_rows.json())
    assert "private query" not in serialized_audit
    assert "private transcript" not in serialized_audit
    assert "token=abc123" not in serialized_audit
    assert "secret=abc123" not in serialized_audit
