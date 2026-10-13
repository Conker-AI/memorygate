"""Relevant old memory survives bounded recall without widening authority."""

from datetime import UTC, datetime, timedelta

import pytest
from app.core.db import Base
from app.models.conversation_receipt import ConversationReceipt
from app.models.memory import Memory
from app.routes import runtime
from app.schemas.runtime import AgentContextRequest
from app.services import lexical_retrieval as lexical
from app.services.scoring import memory_rank_bonus
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def unicode_lower(connection, _record):
        # Production PostgreSQL ILIKE is Unicode aware; SQLite's built-in lower
        # only folds ASCII, so emulate that behavior for deterministic fixtures.
        connection.create_function("lower", 1, lambda value: value.lower() if value else value)

    Base.metadata.create_all(engine)
    monkeypatch.setattr(runtime, "semantic_status", lambda: {
        "status": "degraded", "component": "embeddings", "reason": "synthetic unavailable",
    })
    monkeypatch.setattr(runtime, "build_briefing", lambda *_args: {})
    with Session(engine) as session:
        yield session
    engine.dispose()


def context(db, query, **kwargs):
    return runtime._build_context(db, AgentContextRequest(query=query, **kwargs), "owner")


def add(db, identity, text, **kwargs):
    row = Memory(id=identity, agent_id=kwargs.pop("agent_id", "owner"), text=text, **kwargs)
    db.add(row)
    return row


def test_old_relevant_memory_is_found_after_more_than_200_newer_rows(db):
    now = datetime.now(UTC)
    add(db, "older-relevant", "I prefer quasar scheduling for engineering.",
        summary="Owner's scheduling preference", updated_at=now - timedelta(days=100))
    for index in range(201):
        add(db, f"new-{index:03}", "Unrelated recent note about lunch.", updated_at=now)
    db.add(ConversationReceipt(id="source", agent_id="owner", message_id="msg-original",
                               session_id="chat-original", memory_id="older-relevant", state="admitted"))
    db.commit()

    result = context(db, "Which project uses quasar scheduling?")

    assert [item["id"] for item in result["memories"]] == ["older-relevant"]
    assert result["memories"][0]["citations"] == [{
        "message_id": "msg-original", "session_id": "chat-original", "content_status": "available",
    }]
    assert result["memories"][0]["retrieval_path"] == "lexical"
    assert result["retrieval"]["mode"] == "lexical"
    assert "Semantic retrieval is unavailable" in result["usage"]["instruction"]
    assert result["retrieval"]["lexical"]["candidate_count"] == 1


def test_sql_ranks_before_candidate_limit_and_only_loads_bounded_rows(db):
    now = datetime.now(UTC)
    for index in range(250):
        add(db, f"partial-{index:03}", "A project routine.", updated_at=now)
    add(db, "old-best", "The quasar scheduling project.", updated_at=now - timedelta(days=365))
    db.commit()
    statements = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def record(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    rows = lexical.memory_candidates(db, lexical.prepare_query("project quasar scheduling"), "owner")

    assert len(rows) == lexical.MAX_CANDIDATES
    assert rows[0][0].id == "old-best"
    assert len(statements) == 1 and "LIMIT" in statements[0][0]
    result = context(db, "project quasar scheduling", max_items=1)
    assert [item["id"] for item in result["memories"]] == ["old-best"]
    assert result["retrieval"]["lexical"]["candidate_limit_reached"] is True


def test_namespace_active_state_and_deletion_tombstones_remain_strict(db):
    add(db, "visible", "Quasar scheduling")
    add(db, "foreign", "Quasar scheduling", agent_id="other", memory_type="fact", confidence="high")
    add(db, "deleted", "Quasar scheduling", status="deleted")
    add(db, "review", "Quasar scheduling", status="needs_review")
    add(db, "tombstoned", "Quasar scheduling")
    db.add_all([
        ConversationReceipt(id="deleted-source", agent_id="owner", message_id="gone",
                            memory_id="tombstoned", state="deleted"),
        ConversationReceipt(id="foreign-source", agent_id="other", message_id="foreign-gone",
                            memory_id="visible", state="deleted"),
    ])
    db.commit()
    result = context(db, "What is quasar scheduling?", include_evidence=True)
    assert [item["id"] for item in result["memories"]] == ["visible"]
    assert result["retrieval"]["lexical"]["candidate_count"] == 1
    assert result["memories"][0].get("citations", []) == []


def test_relevance_then_stable_id_order_is_independent_of_recency(db):
    now = datetime.now(UTC)
    add(db, "z-tie", "Quasar scheduling", updated_at=now)
    add(db, "a-tie", "Quasar scheduling", updated_at=now - timedelta(days=30))
    add(db, "partial", "Quasar lunch", updated_at=now)
    add(db, "summary-match", "Something else", summary="Quasar scheduling")
    db.commit()
    first = context(db, "quasar scheduling")["memories"]
    second = context(db, "quasar scheduling")["memories"]
    assert [item["id"] for item in first] == ["a-tie", "summary-match", "z-tie", "partial"]
    assert first == second
    assert first[0]["score"] > first[-1]["score"]


def test_type_and_confidence_weights_match_existing_scoring(db):
    variants = [("fact", "high"), ("watch", "low"), ("skill", "medium"),
                ("context", "high"), ("phase", "low"), ("unknown", "unknown")]
    for index, (kind, confidence) in enumerate(variants):
        add(db, str(index), "quasar", memory_type=kind, confidence=confidence)
    db.commit()
    result = context(db, "quasar")["memories"]
    for item in result:
        assert item["score"] == pytest.approx(0.6 + memory_rank_bonus(item["type"], item["confidence"]))
    assert [item["score"] for item in result] == sorted([item["score"] for item in result], reverse=True)


@pytest.mark.parametrize("query", ["%", "_", "/", "%\u0027 OR \u00271\u0027=\u00271"])
def test_sql_wildcards_and_quotes_are_literal_not_a_broad_search(db, query):
    add(db, "literal", "A literal " + query + " marker")
    add(db, "unrelated", "Ordinary unrelated text")
    db.commit()
    assert [item["id"] for item in context(db, query)["memories"]] == ["literal"]


def test_query_work_and_receipt_are_bounded_without_duplicate_term_weight(db):
    query = "QUASAR quasar, " * 10 + " ".join(f"term{index}" for index in range(100))
    bounded = lexical.prepare_query(query)
    assert len(bounded.terms) == lexical.MAX_TERMS
    assert bounded.terms.count("quasar") == 1 and bounded.terms_limited is True
    oversized = lexical.prepare_query("quasar " + "z" * (lexical.MAX_QUERY_CHARS + 1))
    assert oversized.terms == ("quasar",)
    assert oversized.query_truncated is True and oversized.terms_limited is True
    assert oversized.phrase == ""
    assert len(lexical.prepare_query(" ").terms) == 0
    assert lexical.memory_candidates(db, lexical.prepare_query(" "), "owner") == []
    add(db, "match", "quasar")
    db.commit()
    statements = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def record(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    lexical.memory_candidates(db, bounded, "owner")
    assert len(statements) == 1
    assert len(statements[0][1]) <= lexical.MAX_TERMS * 8 + 64
    result = context(db, "quasar " + "z" * (lexical.MAX_QUERY_CHARS + 1))
    receipt = result["retrieval"]["lexical"]
    assert receipt["query_truncated"] and receipt["terms_limited"]
    assert receipt["terms_used"] == 1 and receipt["candidate_count"] == 1


def test_multilingual_words_and_case_are_preserved(db):
    add(db, "russian", "Предпочитаю Квазар для планирования.")
    db.commit()
    assert [item["id"] for item in context(db, "Какой проект использует квазар?")["memories"]] == ["russian"]


def test_hybrid_search_also_finds_old_unindexed_literal_match(db, monkeypatch):
    add(db, "semantic", "Related vector-only result")
    add(db, "old-literal", "quasar scheduling")
    for index in range(201):
        add(db, f"new-{index:03}", "Unrelated newer note")
    db.commit()
    monkeypatch.setattr(runtime, "semantic_status", lambda: {"status": "ok"})
    monkeypatch.setattr(runtime, "search_memory_embeddings", lambda *_args, **_kwargs: [
        {"id": "semantic", "score": 0.8}, {"id": "old-literal", "score": 0.2},
    ])
    result = context(db, "Which project uses quasar scheduling?")
    assert {item["id"] for item in result["memories"]} == {"semantic", "old-literal"}
    assert len(result["memories"]) == 2 and result["retrieval"]["mode"] == "hybrid"
    # A second search with a sparse index must still find the old lexical row.
    monkeypatch.setattr(runtime, "search_memory_embeddings", lambda *_args, **_kwargs: [
        {"id": "semantic", "score": 0.8},
    ])
    result = context(db, "Which project uses quasar scheduling?")
    assert next(item for item in result["memories"] if item["id"] == "old-literal")["retrieval_path"] == "lexical"
