"""Query text and listener dedupe treat % and _ as plain characters, not SQL wildcards.

Nothing here is mocked away at the storage layer: every assertion runs against a
real SQLite file through the same queries production uses.
"""

from app.core.db import Base
from app.models.entity import Entity
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.routes import entity as entity_routes
from app.routes import memory as memory_routes
from app.services import runtime_pipeline
from app.services.lexical_retrieval import escape_like
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'like-literal.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, sessions


def _client(monkeypatch, sessions):
    app = FastAPI()
    app.include_router(memory_routes.router)
    app.include_router(entity_routes.router)
    monkeypatch.setattr(memory_routes, "SessionLocal", sessions)
    monkeypatch.setattr(entity_routes, "SessionLocal", sessions)
    return TestClient(app)


def _listener_event(agent_id="owner"):
    return EvidenceObject(
        agent_id=agent_id, source_id="source-1", source_key="listener",
        source_type="listener", summary="A bounded event",
        normalized_payload_json='{"content":"A bounded event"}',
    )


def test_escape_like_treats_every_metacharacter_as_literal():
    assert escape_like("a%b_c/d") == "a/%b/_c//d"


def test_memory_search_fallback_matches_wildcards_literally(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    try:
        monkeypatch.setattr(memory_routes, "semantic_status", lambda: {
            "status": "degraded", "component": "vector_index", "reason": "test",
        })
        client = _client(monkeypatch, sessions)
        with sessions() as db:
            db.add(Memory(agent_id="owner", text="50% off", tags_json="[]"))
            db.add(Memory(agent_id="owner", text="nothing here", tags_json="[]"))
            db.commit()

        percent = client.post("/memory/search", json={"agent_id": "owner", "query": "%"})
        assert [row["text"] for row in percent.json()["results"]] == ["50% off"]
        # A lone underscore used to match every memory; it matches no memory here.
        underscore = client.post("/memory/search", json={"agent_id": "owner", "query": "_"})
        assert underscore.json()["results"] == []
    finally:
        engine.dispose()


def test_entity_search_matches_wildcards_literally(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    try:
        client = _client(monkeypatch, sessions)
        with sessions() as db:
            db.add(Entity(agent_id="owner", entity_type="project", name="a_b"))
            db.add(Entity(agent_id="owner", entity_type="project", name="axb"))
            db.commit()

        found = client.post("/entity/search", json={"agent_id": "owner", "query": "a_b"})
        assert [row["name"] for row in found.json()["results"]] == ["a_b"]
    finally:
        engine.dispose()


def test_listener_dedupe_does_not_match_wildcard_lookalikes(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    try:
        monkeypatch.setattr(runtime_pipeline, "score_value", lambda _content: 1.0)
        monkeypatch.setattr(runtime_pipeline, "analyze_evidence", lambda *_args: None)
        with sessions() as db:
            evidence = _listener_event()
            db.add(evidence)
            existing = Memory(agent_id="owner", text="100 is done", tags_json="[]")
            db.add(existing)
            db.commit()
            evidence_id, existing_id = evidence.id, existing.id

        with sessions() as db:
            evidence = db.get(EvidenceObject, evidence_id)
            result = runtime_pipeline.process_evidence(db, evidence, "100% done")

        assert result["memory_id"] != existing_id
        with sessions() as db:
            texts = {row.text for row in db.query(Memory).all()}
        assert texts == {"100 is done", "100% done"}
    finally:
        engine.dispose()


def test_listener_dedupe_tolerates_two_stored_memories_with_equal_text(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    try:
        monkeypatch.setattr(runtime_pipeline, "score_value", lambda _content: 1.0)
        monkeypatch.setattr(runtime_pipeline, "analyze_evidence", lambda *_args: None)
        with sessions() as db:
            evidence = _listener_event()
            db.add(evidence)
            first = Memory(agent_id="owner", text="Same text", tags_json="[]")
            second = Memory(agent_id="owner", text="Same text", tags_json="[]")
            db.add(first)
            db.add(second)
            db.commit()
            evidence_id, stored_ids = evidence.id, {first.id, second.id}

        # This used to raise MultipleResultsFound and fail the processing job.
        with sessions() as db:
            evidence = db.get(EvidenceObject, evidence_id)
            result = runtime_pipeline.process_evidence(db, evidence, "same text")

        assert result["memory_id"] in stored_ids
        with sessions() as db:
            assert db.query(Memory).count() == 2
    finally:
        engine.dispose()
