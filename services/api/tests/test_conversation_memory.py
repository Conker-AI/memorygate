"""Owner statements survive retries; forgetting wins even over a late upload."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from app.core.db import Base
from app.models.analysis_object import AnalysisObject
from app.models.conversation_receipt import ConversationReceipt
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.routes import conversation
from app.services import backup_service
from app.services import conversation_memory as service
from app.services.signal_filter import score_value
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

KEY = "conversation-test-key-long"
HEADERS = {"X-MemoryGate-Conversation-Key": KEY}
TEXTS = ["I prefer to train before school", "Я предпочитаю тренироваться перед школой"]


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memory.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_KEY", KEY)
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_AGENT_ID", "owner")
    yield sessions
    engine.dispose()


@pytest.fixture
def http(database):
    app = FastAPI()
    app.include_router(conversation.router)
    with TestClient(app) as client:
        yield client


def payload(text):
    return {"session_id": "ses_school", "content": text, "created_at": 1770000000.0}


@pytest.mark.parametrize(
    "text",
    [
        *TEXTS,
        "My preference is training before school",
        "I preferred morning training",
        "Люблю плавать",
        "Мне удобнее утром",
        "I like swimming",
    ],
)
def test_real_preferences_are_admitted_with_owner_attribution(http, database, text):
    response = http.put("/runtime/conversation/msg_owner", headers=HEADERS, json=payload(text))
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["state"] == "admitted"
    assert receipt["value_score"] == pytest.approx(0.3)
    with database() as db:
        memory = db.get(Memory, receipt["memory_id"])
        assert memory.text == text
        assert memory.source_type == "owner_statement"
        assert memory.confidence == "medium" and memory.do_not_generalize
        assert db.scalar(select(func.count()).select_from(AnalysisObject)) == 1
        assert db.scalar(select(func.count()).select_from(ObjectLink)) == 2


@pytest.mark.parametrize("text", ["Спасибо!", "ОК", "да", "okay", "thanks", "against"])
def test_acknowledgments_and_substrings_are_not_preferences(text):
    assert score_value(text) == 0


def test_repeated_and_concurrent_delivery_has_one_lineage(database):
    def deliver(_):
        return service.ingest("owner", "msg_same", payload(TEXTS[1]))

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(deliver, range(16)))
    assert len({r["id"] for r in receipts}) == 1
    with database() as db:
        for model in (EvidenceObject, Memory, AnalysisObject, ConversationReceipt):
            assert db.scalar(select(func.count()).select_from(model)) == 1


def test_failed_commit_leaves_no_partial_snapshot(database, monkeypatch):
    from sqlalchemy import event

    engine = database.kw["bind"]

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO memories"):
            raise RuntimeError("simulated storage failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="storage failure"):
            service.ingest("owner", "msg_retry", payload(TEXTS[0]))
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with database() as db:
        for model in (EvidenceObject, Memory, AnalysisObject, ConversationReceipt):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    assert service.ingest("owner", "msg_retry", payload(TEXTS[0]))["state"] == "admitted"


def test_identity_cannot_be_rebound_to_different_content(http):
    assert (
        http.put(
            "/runtime/conversation/msg_one", headers=HEADERS, json=payload(TEXTS[0])
        ).status_code
        == 200
    )
    assert (
        http.put(
            "/runtime/conversation/msg_one", headers=HEADERS, json=payload(TEXTS[1])
        ).status_code
        == 409
    )


def test_deletion_before_upload_and_after_ingestion_is_permanent(http, database):
    for message in ("msg_before", "msg_after"):
        if message == "msg_after":
            assert (
                http.put(
                    f"/runtime/conversation/{message}", headers=HEADERS, json=payload(TEXTS[1])
                ).json()["state"]
                == "admitted"
            )
        assert (
            http.delete(f"/runtime/conversation/{message}", headers=HEADERS).json()["state"]
            == "deleted"
        )
        late = http.put(f"/runtime/conversation/{message}", headers=HEADERS, json=payload(TEXTS[1]))
        assert late.json()["state"] == "deleted"
        assert late.json()["citation"]["content_status"] == "forgotten"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 0
        assert db.scalar(select(func.count()).select_from(AnalysisObject)) == 0
        evidence = db.scalars(select(EvidenceObject)).all()
        assert all(e.summary == "" and e.normalized_payload_json == "{}" for e in evidence)


def test_ingest_credential_cannot_choose_another_agent_or_use_admin_header(http):
    assert (
        http.put(
            "/runtime/conversation/msg_one",
            headers={"X-MemoryGate-Key": KEY},
            json=payload(TEXTS[0]),
        ).status_code
        == 401
    )
    assert (
        http.put(
            "/runtime/conversation/msg_one",
            headers=HEADERS,
            json={**payload(TEXTS[0]), "agent_id": "victim"},
        ).status_code
        == 422
    )
    assert (
        http.put(
            "/runtime/conversation/msg_one",
            headers={**HEADERS, "X-Agent-Id": "victim"},
            json=payload(TEXTS[0]),
        ).status_code
        == 403
    )


def test_failed_index_write_remains_pending_and_tombstone_overrides_it(database, monkeypatch):
    receipt = service.ingest("owner", "msg_index", payload(TEXTS[0]))

    def fail(*args, **kwargs):
        raise ConnectionError("index unavailable")

    monkeypatch.setattr(service.qdrant_store, "upsert_memory_embedding", fail)
    assert service.index_pending() == 0
    with database() as db:
        assert db.get(ConversationReceipt, receipt["id"]).index_pending == "upsert"
    service.forget("owner", "msg_index")
    deleted = []
    monkeypatch.setattr(service.qdrant_store, "delete_memory_embedding", deleted.append)
    assert service.index_pending() == 1
    assert deleted == [receipt["memory_id"]]
    with database() as db:
        assert db.get(ConversationReceipt, receipt["id"]).index_pending == "none"


def test_logical_backup_retains_deletion_tombstones(database, tmp_path, monkeypatch):
    import json

    service.forget("owner", "msg_deleted_before_arrival")
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path))
    with database() as db:
        result = backup_service.create_backup(db)
    tables = json.loads((tmp_path / result["filename"]).read_text(encoding="utf-8"))["tables"]
    assert tables["conversation_receipts"][0]["state"] == "deleted"


def test_short_russian_preference_survives_the_existing_listener_pipeline(database, monkeypatch):
    from app.services import ollama_service, runtime_pipeline

    monkeypatch.setattr(ollama_service, "OLLAMA_ENABLED", False)
    monkeypatch.setattr(ollama_service, "SessionLocal", database)
    with database() as db:
        evidence = EvidenceObject(
            agent_id="owner",
            source_id="test",
            source_key="test",
            source_type="listener",
            summary="Люблю плавать",
        )
        db.add(evidence)
        db.commit()
        result = runtime_pipeline.process_evidence(db, evidence, "Люблю плавать")
        assert result["memory_id"] is not None
        assert db.get(Memory, result["memory_id"]).text == "Люблю плавать"


def test_invalidated_conversation_source_no_longer_supports_active_memory(database, monkeypatch):
    from app.routes import runtime

    monkeypatch.setattr(runtime, "SessionLocal", database)
    receipt = service.ingest("owner", "msg_invalidated", payload(TEXTS[0]))
    result = runtime.invalidate_evidence(
        receipt["id"], "Owner corrected this statement", agent_id="owner"
    )
    assert result["memories_needing_review"] == [receipt["memory_id"]]
    with database() as db:
        assert db.get(Memory, receipt["memory_id"]).status == "needs_review"
    service.forget("owner", "msg_invalidated")
    with database() as db:
        assert db.get(Memory, receipt["memory_id"]) is None
