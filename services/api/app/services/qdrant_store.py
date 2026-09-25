from collections.abc import Callable
from functools import lru_cache
from typing import Any

from app.core.config import EMBED_DIMENSION, QDRANT_COLLECTION, QDRANT_URL
from app.services.embeddings import EmbeddingUnavailable, embed_text, embedding_health
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

OBSERVATION_COLLECTION = f"{QDRANT_COLLECTION}_observations"
ENTITY_COLLECTION = f"{QDRANT_COLLECTION}_entities"

INDEX_UNREACHABLE = "vector index unreachable"

# Collections confirmed to exist. Startup tries to create them, but the API now
# stays up when Qdrant is not ready yet, so every vector operation re-tries
# creation until one succeeds instead of failing forever against a live index.
_ensured: set[str] = set()


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, check_compatibility=False)


def _ensure_collection(name: str) -> None:
    if name in _ensured:
        return
    client = get_qdrant_client()
    collections = client.get_collections().collections
    names = {c.name for c in collections}
    if name not in names:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=EMBED_DIMENSION, distance=Distance.COSINE),
        )
    _ensured.add(name)


def ensure_qdrant_collection() -> None:
    _ensure_collection(QDRANT_COLLECTION)


def ensure_observation_collection() -> None:
    _ensure_collection(OBSERVATION_COLLECTION)


def ensure_entity_collection() -> None:
    _ensure_collection(ENTITY_COLLECTION)


def reset_ensured_collections() -> None:
    """Forget which collections were confirmed, so a test can re-probe."""
    _ensured.clear()


DIMENSION_MISMATCH = "collection vector width does not match the embedding model"


def qdrant_health() -> dict:
    """Reachability, and whether the index can hold what we now produce.

    Reachability alone is not enough. A collection built at one vector width
    cannot accept vectors of another, so an index that is up but shaped for a
    previous model is unusable - and calling it `ok` because the connection
    succeeded is the same confident wrongness this service has been carrying
    elsewhere. Changing the embedding model means rebuilding the collections.
    """
    try:
        client = get_qdrant_client()
        existing = {c.name for c in client.get_collections().collections}
    except Exception:
        return {"status": "unavailable", "reason": INDEX_UNREACHABLE}

    mismatched = []
    unverified = []
    for name in (QDRANT_COLLECTION, OBSERVATION_COLLECTION, ENTITY_COLLECTION):
        if name not in existing:
            continue  # created on first use, at the current dimension
        try:
            size = getattr(client.get_collection(name).config.params.vectors, "size", None)
        except Exception as exc:
            unverified.append(f"{name}: {type(exc).__name__}")
            continue
        if type(size) is not int or size <= 0:
            unverified.append(f"{name}: unknown vector dimension")
        if size is not None and size != EMBED_DIMENSION:
            mismatched.append(f"{name}={size}")

    if unverified:
        return {"status": "degraded", "reason": "Cannot verify vector collections: " + "; ".join(unverified)}
    if mismatched:
        return {
            "status": "degraded",
            "reason": DIMENSION_MISMATCH,
            "expected_dimension": EMBED_DIMENSION,
            "collections": sorted(mismatched),
        }
    return {"status": "ok"}


def semantic_status() -> dict:
    """Can vector retrieval run right now, and if not, which part is down?

    Callers consult this to choose a retrieval path *before* searching, so a
    degraded result is reported as degraded rather than silently swallowed.
    """
    embeddings = embedding_health()
    if embeddings["status"] != "ok":
        return {"status": "degraded", "component": "embeddings", "reason": embeddings["reason"]}
    index = qdrant_health()
    if index["status"] != "ok":
        return {"status": "degraded", "component": "vector_index", "reason": index["reason"]}
    return {"status": "ok", "component": None, "reason": None}


def index_after_commit(upsert: Callable[..., None], *args: Any, **kwargs: Any) -> dict:
    """Run a vector upsert whose row is already committed to Postgres.

    Postgres is the source of truth and has accepted the write, so an embedding
    or index failure must degrade search rather than turn a successful write
    into a 500 with the row already stored. The status is returned instead of
    swallowed, so the caller can say the row is not vector-searchable yet.
    """
    try:
        upsert(*args, **kwargs)
        return {"status": "ok"}
    except EmbeddingUnavailable as exc:
        return {"status": "degraded", "component": "embeddings", "reason": exc.reason}
    except Exception:
        return {"status": "degraded", "component": "vector_index", "reason": INDEX_UNREACHABLE}


def _build_filter(agent_id: str | None = None, extra: dict | None = None) -> Filter | None:
    conditions = []
    if agent_id:
        conditions.append(FieldCondition(key="agent_id", match=MatchValue(value=agent_id)))
    if extra:
        for key, value in extra.items():
            if value is not None:
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
    return Filter(must=conditions) if conditions else None


def upsert_memory_embedding(memory_id: str, text: str, payload: dict | None = None) -> None:
    client = get_qdrant_client()
    _ensure_collection(QDRANT_COLLECTION)
    vector = embed_text(text)
    client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=memory_id,
                vector=vector,
                payload=payload or {},
            )
        ],
    )


def search_memory_embeddings(query: str, limit: int = 20, agent_id: str | None = None) -> list[dict]:
    client = get_qdrant_client()
    _ensure_collection(QDRANT_COLLECTION)
    query_vector = embed_text(query)
    hits = client.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=query_vector,
        query_filter=_build_filter(agent_id),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return [{"id": str(hit.id), "score": float(hit.score)} for hit in hits]


def delete_memory_embedding(memory_id: str) -> None:
    client = get_qdrant_client()
    # Forgetting before the first successful vector write is already satisfied.
    if QDRANT_COLLECTION not in {collection.name for collection in client.get_collections().collections}:
        return
    client.delete(collection_name=QDRANT_COLLECTION, points_selector=[memory_id])


def find_near_duplicate(text: str, limit: int = 3, agent_id: str | None = None) -> list[dict]:
    client = get_qdrant_client()
    _ensure_collection(QDRANT_COLLECTION)
    query_vector = embed_text(text)
    hits = client.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=query_vector,
        query_filter=_build_filter(agent_id),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return [
        {
            "id": str(hit.id),
            "score": float(hit.score),
            "payload": hit.payload or {},
        }
        for hit in hits
    ]


def upsert_observation_embedding(observation_id: str, text: str, payload: dict | None = None) -> None:
    client = get_qdrant_client()
    _ensure_collection(OBSERVATION_COLLECTION)
    vector = embed_text(text)
    client.upsert(
        collection_name=OBSERVATION_COLLECTION,
        points=[
            PointStruct(
                id=observation_id,
                vector=vector,
                payload=payload or {},
            )
        ],
    )


def delete_observation_embedding(observation_id: str) -> None:
    client = get_qdrant_client()
    client.delete(collection_name=OBSERVATION_COLLECTION, points_selector=[observation_id])


def find_similar_observations(
    text: str, agent_id: str, signal_type: str | None = None, limit: int = 3
) -> list[dict]:
    client = get_qdrant_client()
    _ensure_collection(OBSERVATION_COLLECTION)
    query_vector = embed_text(text)
    hits = client.search(
        collection_name=OBSERVATION_COLLECTION,
        query_vector=query_vector,
        query_filter=_build_filter(agent_id, {"signal_type": signal_type}),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return [
        {
            "id": str(hit.id),
            "score": float(hit.score),
            "payload": hit.payload or {},
        }
        for hit in hits
    ]


def upsert_entity_embedding(entity_id: str, text: str, payload: dict | None = None) -> None:
    client = get_qdrant_client()
    _ensure_collection(ENTITY_COLLECTION)
    vector = embed_text(text)
    client.upsert(
        collection_name=ENTITY_COLLECTION,
        points=[
            PointStruct(
                id=entity_id,
                vector=vector,
                payload=payload or {},
            )
        ],
    )


def delete_entity_embedding(entity_id: str) -> None:
    client = get_qdrant_client()
    client.delete(collection_name=ENTITY_COLLECTION, points_selector=[entity_id])


def delete_embeddings(memory_ids: list[str], observation_ids: list[str], entity_ids: list[str]) -> None:
    """Best-effort cleanup used by administrative resets after Postgres is committed."""
    client = get_qdrant_client()
    for collection, ids in (
        (QDRANT_COLLECTION, memory_ids),
        (OBSERVATION_COLLECTION, observation_ids),
        (ENTITY_COLLECTION, entity_ids),
    ):
        if ids:
            client.delete(collection_name=collection, points_selector=ids)


def find_similar_entities(
    text: str, agent_id: str, entity_type: str | None = None, limit: int = 3
) -> list[dict]:
    client = get_qdrant_client()
    _ensure_collection(ENTITY_COLLECTION)
    query_vector = embed_text(text)
    hits = client.search(
        collection_name=ENTITY_COLLECTION,
        query_vector=query_vector,
        query_filter=_build_filter(agent_id, {"entity_type": entity_type}),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return [
        {
            "id": str(hit.id),
            "score": float(hit.score),
            "payload": hit.payload or {},
        }
        for hit in hits
    ]
