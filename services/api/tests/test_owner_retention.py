"""Explicit owner admission is separate from heuristics and source deletion."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.core.migrations import migrate_conversation_admission
from app.models.analysis_object import AnalysisObject
from app.models.conversation_receipt import ConversationReceipt
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.processing_job import ProcessingJob
from app.routes import runtime
from app.schemas.runtime import AgentContextRequest
from app.services import backup_service
from app.services import conversation_memory as service
from app.services.owner_retention import retention_intent
from app.services.signal_filter import score_value
from sqlalchemy import create_engine, func, select, text
from test_conversation_memory import HEADERS, KEY, payload
from test_conversation_memory import database as database
from test_conversation_memory import http as http


@pytest.mark.parametrize(
    "content",
    [
        "Remember that my name is Mira",
        "Please remember that I live in Haifa",
        "Please, REMEMBER THAT I live in Haifa",
        "Remember: use short answers",
        "Remember this: my name is Mira",
        "Запомни, что меня зовут Мира",
        "Пожалуйста, запомните, что меня зовут Мира",
        "Запомни что меня зовут Мира",
        "Запомни: отвечай коротко",
        "Запомните это: меня зовут Мира",
        "Ｒｅｍｅｍｂｅｒ ｔｈａｔ my name is Mira",
        "Remember this: 🐈",
    ],
)
def test_explicit_directives_admit_exact_owner_statement_without_inventing_score(
    http,
    database,
    content,
):
    assert score_value(content) == 0
    result = http.put("/runtime/conversation/msg_explicit", headers=HEADERS, json=payload(content))
    assert result.status_code == 200
    receipt = result.json()
    assert receipt["state"] == "admitted" and receipt["admission_method"] == "owner_requested"
    assert receipt["value_score"] == 0
    with database() as db:
        memory = db.get(Memory, receipt["memory_id"])
        assert memory.text == content and memory.source_type == "owner_statement"
        assert memory.confidence == "medium" and memory.do_not_generalize
        evidence = db.get(EvidenceObject, receipt["id"])
        assert json.loads(evidence.normalized_payload_json) == {"content": content}
        analysis = db.scalars(select(AnalysisObject)).one()
        assert json.loads(analysis.steps_json) == [
            "owner_requested_retention",
            "retain_owner_attribution",
        ]
        memories = [{"id": memory.id}]
        service.citations(db, memories, "owner")
        assert memories[0]["citations"] == [
            {
                "message_id": "msg_explicit",
                "session_id": "ses_school",
                "content_status": "available",
            }
        ]


@pytest.mark.parametrize(
    "content",
    [
        "Remember that ",
        "Remember that ...",
        "Remember:",
        "Remember this:  ",
        "Запомни, что ",
        "Запомни это: ...",
        "Remember to buy milk",
        '"Remember that my name is Mira"',
        "> Remember that my name is Mira",
        "```\nRemember that my name is Mira\n```",
        "    Remember that my name is Mira",
        "\tRemember that my name is Mira",
        "My friend said: Remember that my name is Mira",
        "Can you remember that my name is Mira?",
        "Remember that my name is Mira?",
        "Помнишь, как меня зовут?",
        "My name is Mira",
        "I live in Haifa",
        "Use short answers",
        "Меня зовут Мира",
    ],
)
def test_other_factual_quoted_indirect_or_empty_text_does_not_gain_explicit_admission(
    http, content
):
    assert retention_intent(content) is None
    receipt = http.put(
        "/runtime/conversation/msg_other", headers=HEADERS, json=payload(content)
    ).json()
    admitted = score_value(content) >= 0.3
    assert receipt["admission_method"] == ("signal" if admitted else "filtered")
    assert receipt["state"] == ("admitted" if admitted else "filtered")
    assert (receipt["memory_id"] is not None) == admitted


@pytest.mark.parametrize(
    "content",
    [
        "I prefer short answers",
        '"Remember that I prefer short answers"',
        "Can you remember that I prefer short answers?",
        "Я живу в Хайфе",
    ],
)
def test_ordinary_signal_admission_is_unchanged_and_never_labeled_owner_requested(http, content):
    receipt = http.put(
        "/runtime/conversation/msg_signal", headers=HEADERS, json=payload(content)
    ).json()
    assert receipt["state"] == "admitted" and receipt["admission_method"] == "signal"
    assert receipt["value_score"] == score_value(content)


@pytest.mark.parametrize(
    "content",
    [
        "Do not remember that I prefer short answers",
        "Please don't remember that I prefer short answers",
        "Please, don’t remember that I prefer short answers",
        "Never remember that I prefer short answers",
        "Не запоминай, что я предпочитаю короткие ответы",
        "Пожалуйста, не запоминайте, что я предпочитаю короткие ответы",
    ],
)
def test_direct_decline_filters_even_high_signal_without_scheduling_later_promotion(
    http,
    database,
    monkeypatch,
    content,
):
    assert score_value(content) >= 0.3
    receipt = http.put(
        "/runtime/conversation/msg_decline", headers=HEADERS, json=payload(content)
    ).json()
    assert receipt["state"] == "filtered" and receipt["admission_method"] == "filtered"
    assert receipt["value_score"] == score_value(content) and receipt["memory_id"] is None
    assert receipt["index_pending"] == "none"
    monkeypatch.setattr(
        runtime, "semantic_status", lambda: {"status": "not_configured", "reason": "isolated"}
    )
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 0
        assert db.scalar(select(func.count()).select_from(ProcessingJob)) == 0
        evidence = db.get(EvidenceObject, receipt["id"])
        assert evidence.processing_state == "processed"
        assert json.loads(evidence.normalized_payload_json) == {"content": content}
        assert "owner_declined_retention" in json.loads(
            db.scalars(select(AnalysisObject)).one().steps_json
        )
        package = runtime._build_context(
            db, AgentContextRequest(query="answers короткие", include_evidence=False), "owner"
        )
        assert package["memories"] == [] and package["evidence"] == []
        # Filtering is not deletion: an authorized raw-evidence read remains possible.
        evidence_package = runtime._build_context(
            db, AgentContextRequest(query="answers короткие", include_evidence=True), "owner"
        )
        assert [row["id"] for row in evidence_package["evidence"]] == [receipt["id"]]


def test_concurrent_retry_is_single_admission_with_original_fingerprint(database):
    body = payload("Remember that my name is Mira")
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: service.ingest("owner", "msg_same", body), range(8)))
    assert all(receipt == receipts[0] for receipt in receipts)
    with database() as db:
        row = db.get(ConversationReceipt, receipts[0]["id"])
        assert (
            row.fingerprint
            == hashlib.sha256(
                json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
        )
        assert db.scalar(select(func.count()).select_from(Memory)) == 1
    with pytest.raises(Exception) as failure:
        service.ingest("owner", "msg_same", payload("Remember that my name is June"))
    assert failure.value.status_code == 409


def test_same_words_from_listener_do_not_gain_owner_retention_authority(database, monkeypatch):
    from app.services import ollama_service, runtime_pipeline

    content = "Remember that my name is Mira"
    monkeypatch.setattr(ollama_service, "OLLAMA_ENABLED", False)
    monkeypatch.setattr(ollama_service, "SessionLocal", database)
    with database() as db:
        evidence = EvidenceObject(
            agent_id="owner",
            source_id="listener",
            source_key="listener",
            source_type="listener",
            summary=content,
        )
        db.add(evidence)
        db.commit()
        result = runtime_pipeline.process_evidence(db, evidence, content)
        assert result["value_score"] == 0 and result["memory_id"] is None
        assert db.scalar(select(func.count()).select_from(Memory)) == 0


def test_old_filtered_receipt_is_not_reinterpreted_or_rewritten(database, monkeypatch):
    body = payload("Remember that my name is Mira")
    with monkeypatch.context() as legacy:
        legacy.setattr(service, "retention_intent", lambda _: None)
        old = service.ingest("owner", "msg_legacy", body)
    with database() as db:
        row = db.get(ConversationReceipt, old["id"])
        row.admission_method = None
        db.commit()
        before = {
            column.name: getattr(row, column.name)
            for column in ConversationReceipt.__table__.columns
        }
    assert service.ingest("owner", "msg_legacy", body) == {**old, "admission_method": None}
    with database() as db:
        row = db.get(ConversationReceipt, old["id"])
        assert {
            column.name: getattr(row, column.name)
            for column in ConversationReceipt.__table__.columns
        } == before
        assert db.scalar(select(func.count()).select_from(Memory)) == 0


@pytest.mark.parametrize("first_ingest", [False, True])
def test_tombstone_wins_over_explicit_remember_and_late_replay(http, database, first_ingest):
    body = payload("Remember that my name is Mira")
    if first_ingest:
        http.put("/runtime/conversation/msg_deleted", headers=HEADERS, json=body)
    forgotten = http.delete("/runtime/conversation/msg_deleted", headers=HEADERS).json()
    late = http.put("/runtime/conversation/msg_deleted", headers=HEADERS, json=body).json()
    assert late == forgotten and late["state"] == "deleted"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Memory)) == 0
        assert db.scalar(select(func.count()).select_from(AnalysisObject)) == 0


def test_explicit_text_does_not_change_credentials_namespace_or_payload_authority(http, database):
    body = payload("Remember that my name is Mira")
    for headers in ({}, {"X-MemoryGate-Key": KEY}, {"X-MemoryGate-Correction-Key": KEY}):
        assert (
            http.put("/runtime/conversation/msg_denied", headers=headers, json=body).status_code
            == 401
        )
    assert (
        http.put(
            "/runtime/conversation/msg_denied",
            headers={**HEADERS, "X-Agent-Id": "other"},
            json=body,
        ).status_code
        == 403
    )
    for extra in ({"admission_method": "owner_requested"}, {"force": True}, {"agent_id": "other"}):
        assert (
            http.put(
                "/runtime/conversation/msg_denied", headers=HEADERS, json={**body, **extra}
            ).status_code
            == 422
        )
    with database() as db:
        assert db.scalar(select(func.count()).select_from(ConversationReceipt)) == 0


def test_nullable_migration_is_repeatable_and_preserves_existing_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "CREATE TABLE conversation_receipts (id TEXT PRIMARY KEY, state TEXT, fingerprint TEXT)"
                )
            )
            db.execute(
                text("INSERT INTO conversation_receipts VALUES ('old','filtered','original')")
            )
            migrate_conversation_admission(db)
            migrate_conversation_admission(db)
            assert dict(
                db.execute(text("SELECT * FROM conversation_receipts")).mappings().one()
            ) == {
                "id": "old",
                "state": "filtered",
                "fingerprint": "original",
                "admission_method": None,
            }
    finally:
        engine.dispose()


def test_logical_backup_preserves_durable_admission_method(database, tmp_path, monkeypatch):
    service.ingest("owner", "msg_backup", payload("Remember that my name is Mira"))
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path))
    with database() as db:
        receipt = backup_service.create_backup(db)
    backup = json.loads((tmp_path / receipt["filename"]).read_text(encoding="utf-8"))
    assert backup["tables"]["conversation_receipts"][0]["admission_method"] == "owner_requested"
