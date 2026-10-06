"""Probe results stay complete and stable while other requests invalidate them."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from app.services import embeddings


@pytest.fixture(autouse=True)
def provider_cache(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBEDDINGS_KEY", "qa-test-only")
    embeddings.reset_provider_cache()
    yield
    embeddings.reset_provider_cache()


def model_response(body=None):
    return httpx.Response(
        200, json=body if body is not None else {"model": "qa-model", "dimension": 8},
        request=httpx.Request("GET", "http://qa.invalid/model"),
    )


def test_invalidation_does_not_mutate_a_probe_already_returned(monkeypatch):
    monkeypatch.setattr(embeddings.httpx, "get", lambda *args, **kwargs: model_response())
    snapshot = embeddings._probe()
    embeddings.reset_provider_cache()
    assert snapshot["model"] == "qa-model"
    assert snapshot["dimension"] == 8


def test_probe_never_publishes_a_partial_result(monkeypatch):
    entered, release = Event(), Event()

    def blocked_get(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return model_response()

    monkeypatch.setattr(embeddings.httpx, "get", blocked_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(embeddings.embedding_health)
        try:
            assert entered.wait(5)
            assert embeddings._provider == {}
            second = pool.submit(embeddings.embedding_health)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == {
            "status": "ok", "model": "qa-model", "dimension": 8,
        }


@pytest.mark.parametrize("body", [{}, {"model": "qa-model"}, {"model": "qa-model", "dimension": "bad"}])
def test_malformed_probe_reports_unavailability_without_raising(monkeypatch, body):
    monkeypatch.setattr(embeddings.httpx, "get", lambda *args, **kwargs: model_response(body))
    health = embeddings.embedding_health()
    assert health["status"] == "unavailable"
    assert health["reason"].startswith("embedding sidecar unavailable:")
    with pytest.raises(embeddings.EmbeddingUnavailable):
        embeddings.embed_text("fictional QA text")


def test_failed_embedding_invalidates_future_probes_not_existing_snapshots(monkeypatch):
    monkeypatch.setattr(embeddings.httpx, "get", lambda *args, **kwargs: model_response())
    snapshot = embeddings._probe()

    def fail_embed(*args, **kwargs):
        raise httpx.ConnectError("QA provider unavailable")

    monkeypatch.setattr(embeddings.httpx, "post", fail_embed)
    with pytest.raises(embeddings.EmbeddingUnavailable):
        embeddings.embed_text("fictional QA text")
    assert embeddings._provider == {}
    assert snapshot["model"] == "qa-model"
