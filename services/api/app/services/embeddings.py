"""Embedding access, with an honest answer when no provider is available.

Vectors come from the Embeddings sidecar over HTTP. MemoryGate used to load a
model inside this process, which tied a web API's startup to a model load and
made the provider unswappable; behind an HTTP contract the model is a
deployment choice. See ADR-0004 and ADR-0007 in the Conker repository.

MemoryGate previously also shipped an `EMBED_MODEL=hash` mode that derived every
vector component from `sha256(f"{index}:{text}")`. That is a hash of the text,
not a representation of it: near-identical sentences produced uncorrelated
vectors, so cosine similarity over them was noise. Retrieval looked like it
worked and silently returned confident nonsense. It is gone.

There is deliberately no fallback here. When the sidecar cannot produce a
vector this module raises `EmbeddingUnavailable`, and callers degrade to
lexical retrieval *and say so*.
"""

import os
import time
from threading import Lock
from typing import Any

import httpx

REMOVED_HASH_MODEL = "hash"

EMBEDDINGS_URL = os.environ.get("EMBEDDINGS_URL", "http://embeddings-api:8030").rstrip("/")
EMBEDDINGS_KEY = os.environ.get("EMBEDDINGS_KEY", "")
_TIMEOUT = float(os.environ.get("EMBEDDINGS_TIMEOUT_SECONDS", "30"))

# A failed probe is cached briefly too: a missing sidecar must not cost a
# connection attempt on every single retrieval. Short, because the sidecar
# coming back should be noticed without a restart.
_PROBE_TTL_SECONDS = 30.0
_provider: dict[str, Any] = {}
_provider_lock = Lock()


class EmbeddingUnavailable(RuntimeError):
    """No embedding provider can produce a vector right now."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _headers() -> dict[str, str]:
    return {"X-Embeddings-Key": EMBEDDINGS_KEY} if EMBEDDINGS_KEY else {}


def _probe() -> dict[str, Any]:
    """Ask the sidecar what it is. Cached, and never raises."""
    # Publish only complete probes and return a snapshot: embedding failures in
    # another request must not erase the health caller's provider identity.
    with _provider_lock:
        now = time.monotonic()
        if _provider and now - _provider.get("at", 0.0) < _PROBE_TTL_SECONDS:
            return dict(_provider)

        result: dict[str, Any] = {"at": now}
        if not EMBEDDINGS_KEY:
            result["reason"] = "EMBEDDINGS_KEY is not set, so the embedding sidecar cannot be called"
        else:
            try:
                response = httpx.get(
                    f"{EMBEDDINGS_URL}/model",
                    headers=_headers(),
                    timeout=5.0,
                    trust_env=False,
                    follow_redirects=False,
                )
                response.raise_for_status()
                body = response.json()
                model, dimension = body["model"], int(body["dimension"])
                if not isinstance(model, str) or not model or dimension <= 0:
                    raise ValueError("invalid embedding provider identity")
                result.update(model=model, dimension=dimension)
            except Exception as exc:
                result["reason"] = f"embedding sidecar unavailable: {type(exc).__name__}"
        _provider.clear()
        _provider.update(result)
        return dict(result)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch, or raise `EmbeddingUnavailable`. Never returns noise."""
    if not texts:
        return []
    provider = _probe()
    if "model" not in provider:
        raise EmbeddingUnavailable(provider["reason"])
    try:
        response = httpx.post(
            f"{EMBEDDINGS_URL}/embed",
            json={"texts": texts},
            headers=_headers(),
            timeout=_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        vectors = response.json()["vectors"]
    except Exception as exc:
        # Forget the cached identity. Whatever is wrong, the next caller should
        # re-probe rather than inherit a stale belief that the sidecar is fine.
        reset_provider_cache()
        raise EmbeddingUnavailable(f"embedding request failed: {type(exc).__name__}") from exc

    if len(vectors) != len(texts):
        reset_provider_cache()
        raise EmbeddingUnavailable("embedding sidecar returned an incomplete batch")
    return vectors


def embed_text(text: str) -> list[float]:
    """Embed one string, or raise `EmbeddingUnavailable`."""
    return embed_texts([text])[0]


def provider_dimension() -> int | None:
    """The sidecar's vector width, or None if it cannot be reached."""
    provider = _probe()
    return provider.get("dimension")


def embedding_health() -> dict:
    """Coarse provider status for `/health`. Never raises, never guesses."""
    provider = _probe()
    if "model" in provider:
        return {"status": "ok", "model": provider["model"], "dimension": provider["dimension"]}
    return {"status": "unavailable", "reason": provider["reason"]}


def reset_provider_cache() -> None:
    """Forget a cached probe, so a test can re-probe after reconfiguring."""
    with _provider_lock:
        _provider.clear()
