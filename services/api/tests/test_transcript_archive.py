"""Transcripts live only in Pi: the legacy archive takes no new ones and can be emptied."""

from app.core import auth
from app.core.db import Base
from app.models.audit import MemoryAudit
from app.models.deletion_receipt import DeletionReceipt
from app.models.object_link import ObjectLink
from app.models.session_transcript import SessionTranscript
from app.routes import transcript
from app.services import deletion_recovery as recovery
from app.services.auth_settings_service import set_admin_key
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ADMIN = "Admin-key-123!"


def _stack(tmp_path, monkeypatch, name="archive.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(auth, "SessionLocal", Session)
    monkeypatch.setattr(transcript, "SessionLocal", Session)
    with Session() as db:
        set_admin_key(db, ADMIN)
        db.add_all([
            SessionTranscript(id="t-owner", agent_id="owner", transcript="owner: my secret plan"),
            SessionTranscript(id="t-other", agent_id="other", transcript="other: theirs"),
        ])
        db.commit()
    app = FastAPI()
    app.include_router(transcript.router, dependencies=[Depends(auth.require_key)])
    return TestClient(app), Session


def _headers(agent="owner"):
    return {"X-MemoryGate-Key": ADMIN, "X-Agent-Id": agent}


def test_a_new_transcript_is_refused_and_nothing_is_stored(tmp_path, monkeypatch):
    client, Session = _stack(tmp_path, monkeypatch)

    response = client.post("/transcripts", headers=_headers(),
                           json={"session_id": "s1", "transcript": "owner: hello"})

    assert response.status_code in (404, 405)
    with Session() as db:
        assert sorted(db.scalars(select(SessionTranscript.id))) == ["t-other", "t-owner"]


def test_soulgate_processing_flags_are_gone(tmp_path, monkeypatch):
    client, _ = _stack(tmp_path, monkeypatch)

    for path in ("/transcripts/t-owner/reprocess", "/transcripts/t-owner/mark-processed"):
        assert client.post(path, headers=_headers()).status_code in (404, 405)


def test_deleting_a_transcript_removes_its_text_and_keeps_a_content_free_receipt(tmp_path, monkeypatch):
    client, Session = _stack(tmp_path, monkeypatch)
    with Session() as db:
        db.add(ObjectLink(id="l1", source_type="transcript", source_id="t-owner", target_type="memory",
                          target_id="m1", relationship="supports", metadata_json="{}", created_by="test"))
        db.commit()

    deleted = client.delete("/transcripts/t-owner", headers=_headers())

    assert deleted.json() == {"status": "ok"}
    assert client.get("/transcripts/t-owner/full", headers=_headers()).status_code == 404
    assert client.get("/transcripts/owner", headers=_headers()).json() == {"results": []}
    with Session() as db:
        assert db.get(SessionTranscript, "t-owner") is None
        assert db.get(DeletionReceipt, ("owner", "transcript", "t-owner")) is not None
        assert db.get(ObjectLink, "l1") is None
        assert db.get(DeletionReceipt, ("", "link", "l1")) is not None
        audit = db.scalars(select(MemoryAudit.payload_json)).all()
    assert audit and all("secret plan" not in row for row in audit)
    assert client.delete("/transcripts/t-owner", headers=_headers()).status_code == 404


def test_another_agents_transcript_cannot_be_read_or_deleted(tmp_path, monkeypatch):
    client, Session = _stack(tmp_path, monkeypatch)

    assert client.get("/transcripts/t-other/full", headers=_headers()).status_code == 404
    assert client.delete("/transcripts/t-other", headers=_headers()).status_code == 404
    with Session() as db:
        assert db.get(SessionTranscript, "t-other") is not None


def test_a_restored_backup_does_not_bring_a_deleted_transcript_back(tmp_path, monkeypatch):
    client, Session = _stack(tmp_path, monkeypatch)
    client.delete("/transcripts/t-owner", headers=_headers())
    with Session() as db:
        receipts = recovery.evidence(db)
    assert ("owner", "transcript", "t-owner") in receipts

    restored_engine = create_engine(f"sqlite:///{tmp_path / 'restored.db'}")
    Base.metadata.create_all(restored_engine)
    restored = sessionmaker(restored_engine, autoflush=False, expire_on_commit=False)
    with restored() as db:
        db.add(SessionTranscript(id="t-owner", agent_id="owner", transcript="owner: my secret plan"))
        db.commit()

    recovery.replay(receipts, restored)

    with restored() as db:
        assert db.get(SessionTranscript, "t-owner") is None
    restored_engine.dispose()
