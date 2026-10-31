"""Entity dedup reports honestly what the similarity tier could and could not check."""

import pytest
from app.core.db import Base
from app.models.entity import Entity
from app.services import entity_dedup
from app.services.qdrant_store import INDEX_UNREACHABLE
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

OK = {"status": "ok", "component": None, "reason": None}


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_entity(db, identity, name, agent_id="owner", entity_type="person"):
    row = Entity(id=identity, agent_id=agent_id, entity_type=entity_type, name=name)
    db.add(row)
    return row


def test_exact_name_match_ignoring_case_and_surrounding_spaces_needs_no_vector(db):
    stored = add_entity(db, "e1", "  Alice Chen ")
    db.flush()

    found, status = entity_dedup.find_duplicate(db, "owner", "person", "  aLiCe chEn ")

    assert found is stored
    assert status == {"status": "ok", "component": None, "reason": None}


def test_same_name_with_other_type_or_agent_is_not_a_duplicate(db, monkeypatch):
    add_entity(db, "e1", "Alice Chen", agent_id="owner", entity_type="person")
    monkeypatch.setattr(entity_dedup, "semantic_status", lambda: OK)
    searched = []
    monkeypatch.setattr(entity_dedup, "find_similar_entities", lambda *args, **kwargs: searched.append(kwargs) or [])
    db.flush()

    found, status = entity_dedup.find_duplicate(db, "owner", "project", "Alice Chen")
    assert found is None and status == OK
    found, status = entity_dedup.find_duplicate(db, "other-agent", "person", "Alice Chen")
    assert found is None and status == OK
    # Neither exact tier matched, so the similarity search ran for both calls.
    assert len(searched) == 2


def test_non_ok_status_returns_none_without_searching(db, monkeypatch):
    unavailable = {"status": "unavailable", "component": "vector_index", "reason": INDEX_UNREACHABLE}
    monkeypatch.setattr(entity_dedup, "semantic_status", lambda: unavailable)

    def must_not_search(*args, **kwargs):
        raise AssertionError("the similarity search must not run when the vector path is down")

    monkeypatch.setattr(entity_dedup, "find_similar_entities", must_not_search)

    found, status = entity_dedup.find_duplicate(db, "owner", "person", "Alice Chen")

    assert found is None
    assert status == unavailable


def test_failing_search_reports_a_degraded_vector_index(db, monkeypatch):
    monkeypatch.setattr(entity_dedup, "semantic_status", lambda: OK)

    def broken_search(*args, **kwargs):
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(entity_dedup, "find_similar_entities", broken_search)

    found, status = entity_dedup.find_duplicate(db, "owner", "person", "Alice Chen")

    assert found is None
    assert status == {"status": "degraded", "component": "vector_index", "reason": INDEX_UNREACHABLE}


def test_similarity_tier_respects_the_threshold(db, monkeypatch):
    # Stored under a name that the exact tier will not match, so the call
    # actually reaches the similarity search.
    stored = add_entity(db, "e1", "Alicia Chen")
    db.flush()
    monkeypatch.setattr(entity_dedup, "semantic_status", lambda: OK)

    def search_returning(score):
        def search(*args, **kwargs):
            return [{"id": "e1", "score": score}]

        return search

    monkeypatch.setattr(entity_dedup, "find_similar_entities", search_returning(0.9))
    found, status = entity_dedup.find_duplicate(db, "owner", "person", "Alice Chen")
    assert found is stored
    assert status == OK

    monkeypatch.setattr(entity_dedup, "find_similar_entities", search_returning(0.89))
    found, status = entity_dedup.find_duplicate(db, "owner", "person", "Alice Chen")
    assert found is None
    assert status == OK
