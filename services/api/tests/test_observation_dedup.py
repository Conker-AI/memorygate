"""Observation dedup is purely vector-based, so its status must say when nothing was checked."""

from app.services import observation_lifecycle
from app.services.qdrant_store import INDEX_UNREACHABLE

OK = {"status": "ok", "component": None, "reason": None}


def test_non_ok_status_returns_none_without_searching(monkeypatch):
    unavailable = {"status": "unavailable", "component": "vector_index", "reason": INDEX_UNREACHABLE}
    monkeypatch.setattr(observation_lifecycle, "semantic_status", lambda: unavailable)

    def must_not_search(*args, **kwargs):
        raise AssertionError("the similarity search must not run when the vector path is down")

    monkeypatch.setattr(observation_lifecycle, "find_similar_observations", must_not_search)

    duplicate, status = observation_lifecycle.find_duplicate("owner", "verbal", "some description")

    assert duplicate is None
    assert status == unavailable


def test_failing_search_reports_a_degraded_vector_index(monkeypatch):
    monkeypatch.setattr(observation_lifecycle, "semantic_status", lambda: OK)

    def broken_search(*args, **kwargs):
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(observation_lifecycle, "find_similar_observations", broken_search)

    duplicate, status = observation_lifecycle.find_duplicate("owner", "verbal", "some description")

    assert duplicate is None
    assert status == {"status": "degraded", "component": "vector_index", "reason": INDEX_UNREACHABLE}


def test_score_at_the_threshold_is_a_duplicate_and_just_below_is_not(monkeypatch):
    monkeypatch.setattr(observation_lifecycle, "semantic_status", lambda: OK)
    hit = {"id": "obs-1", "score": 0.85, "description": "the same signal"}
    threshold = observation_lifecycle.DEDUP_SIMILARITY_THRESHOLD

    def search_returning(score):
        return lambda *args, **kwargs: [dict(hit, score=score)]

    monkeypatch.setattr(observation_lifecycle, "find_similar_observations", search_returning(threshold))
    duplicate, status = observation_lifecycle.find_duplicate("owner", "verbal", "the same signal")
    assert duplicate == hit
    assert status == OK

    monkeypatch.setattr(observation_lifecycle, "find_similar_observations", search_returning(threshold - 0.01))
    duplicate, status = observation_lifecycle.find_duplicate("owner", "verbal", "the same signal")
    assert duplicate is None
    assert status == OK
