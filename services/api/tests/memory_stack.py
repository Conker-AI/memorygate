"""A whole MemoryGate behind its real HTTP API, on throwaway stores.

The app is `app.main.app`, with every router and every auth dependency. The
source of truth is a temporary SQLite file. The vector index is Qdrant's own
in-process local mode, behind a switch that makes it unreachable. The embedding
sidecar is a small word-bucket embedder with a few synonyms, so a semantic hit
can find what literal word matching cannot. Nothing touches the network.

Tests import the `stack` fixture from here; this file is not collected itself.
"""

import hashlib
import math
import re
import sys
import threading
from datetime import datetime, timedelta

import pytest
from app import main
from app.core import db as core_db
from app.core.db import Base
from app.services import (
    auth_settings_service,
    backup_service,
    conversation_memory,
    qdrant_store,
    vector_outbox,
)
from app.services.embeddings import EmbeddingUnavailable
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

OWNER = "owner"
ADMIN_KEY = "boundary-admin-key-not-a-secret"
READ_KEY = "mg_read_boundary_owner_key_not_a_secret"
CORRECTION_KEY = "boundary-correction-key-not-a-secret-0001"
CONVERSATION_KEY = "boundary-conversation-key-not-a-secret"
DIMENSION = 256
# The embedder's only "meaning": these words land where their synonym does.
SYNONYMS = {"espresso": "coffee", "latte": "coffee", "puppy": "dog", "hound": "dog"}
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def embed(value: str) -> list[float]:
    vector = [0.0] * DIMENSION
    for word in _WORDS.findall(value.casefold()):
        word = SYNONYMS.get(word, word)
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSION] += 1.0
    norm = math.sqrt(sum(part * part for part in vector)) or 1.0
    return [part / norm for part in vector]


def request_id(label: str) -> str:
    """A request identity in the correction capability's shape (16+ characters)."""
    return f"req_{label}".ljust(16, "0")


class VectorIndex:
    """Qdrant local mode. `down = True` makes every call fail like a refused connection."""

    def __init__(self):
        self.client = QdrantClient(":memory:")
        self.down = False
        self.failing: set[str] = set()  # calls that fail while the rest still answer
        self.upserted: list[str] = []
        self._lock = threading.Lock()

    def __getattr__(self, name):
        method = getattr(self.client, name)

        def call(*args, **kwargs):
            if self.down or name in self.failing:
                raise ConnectionError("connection refused")
            with self._lock:
                if name == "upsert":
                    self.upserted.extend(str(point.id) for point in kwargs["points"])
                return method(*args, **kwargs)

        return call

    def vectors(self, collection: str = qdrant_store.QDRANT_COLLECTION) -> dict[str, list[float]]:
        """Every stored point, read straight from the index rather than through MemoryGate."""
        with self._lock:
            if collection not in {item.name for item in self.client.get_collections().collections}:
                return {}
            points, _ = self.client.scroll(collection, limit=10_000, with_vectors=True)
        return {str(point.id): list(point.vector) for point in points}

    def holds(self, memory_id: str, value: str) -> bool:
        stored = self.vectors().get(memory_id)
        return stored is not None and all(
            math.isclose(a, b, abs_tol=1e-5) for a, b in zip(stored, embed(value), strict=True)
        )


class Clock(datetime):
    """The outbox's clock, so a test can let the retry backoff run out."""

    offset = timedelta(0)

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + cls.offset


class Stack:
    def __init__(self, http, sessions, engine, index, embeddings):
        self.http, self.sessions, self.engine = http, sessions, engine
        self.index, self.embeddings = index, embeddings
        self.companion_key = ""

    # --- credentials ------------------------------------------------------

    @staticmethod
    def admin_headers(agent: str = OWNER) -> dict:
        return {"X-MemoryGate-Key": ADMIN_KEY, "X-Agent-Id": agent}

    @staticmethod
    def read_headers(key: str = READ_KEY, agent: str = OWNER) -> dict:
        return {"X-MemoryGate-Key": key, "X-Agent-Id": agent}

    CORRECTION = {"X-MemoryGate-Correction-Key": CORRECTION_KEY}
    CONVERSATION = {"X-MemoryGate-Conversation-Key": CONVERSATION_KEY}

    @staticmethod
    def clear_lockouts() -> None:
        """Forget failed attempts, so one test can check many refusals without a lockout."""
        auth_settings_service._attempt_state.clear()

    # --- writes -----------------------------------------------------------

    def write(self, value: str, agent: str = OWNER, **body) -> dict:
        response = self.http.post(
            "/memory/write", headers=self.admin_headers(agent), json={"text": value, **body}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def say(self, message_id: str, value: str, session: str = "ses_one") -> dict:
        """Pi delivers one owner chat message."""
        response = self.http.put(
            f"/runtime/conversation/{message_id}",
            headers=self.CONVERSATION,
            json={"session_id": session, "content": value, "created_at": 1770000000.0},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def unsay(self, message_id: str) -> dict:
        response = self.http.delete(
            f"/runtime/conversation/{message_id}", headers=self.CONVERSATION
        )
        assert response.status_code == 200, response.text
        return response.json()

    def remember(self, label: str, value: str) -> dict:
        response = self.http.put(
            f"/runtime/corrections/remember/{request_id(label)}",
            headers=self.CORRECTION,
            json={"text": value},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def correct(self, label: str, memory_id: str, revision: int, value: str):
        return self.http.put(
            f"/runtime/corrections/{request_id(label)}",
            headers=self.CORRECTION,
            json={"memory_id": memory_id, "expected_revision": revision, "text": value},
        )

    def forget(self, label: str, memory_id: str, revision: int):
        return self.http.put(
            f"/runtime/corrections/forget/{request_id(label)}",
            headers=self.CORRECTION,
            json={"memory_id": memory_id, "expected_revision": revision},
        )

    # --- reads ------------------------------------------------------------

    def search(self, query: str, agent: str = OWNER) -> dict:
        response = self.http.post(
            "/memory/search", headers=self.admin_headers(agent), json={"query": query}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def runtime(self, route: str, body: dict, key: str = READ_KEY, agent: str = OWNER):
        """POST /runtime/{context,library,explore} with a read key, the way Pi calls them."""
        return self.http.post(f"/runtime/{route}", headers=self.read_headers(key, agent), json=body)

    def context(self, query: str, **body) -> dict:
        response = self.runtime("context", {"query": query, **body})
        assert response.status_code == 200, response.text
        return response.json()

    def library(self, **body) -> dict:
        response = self.runtime("library", body)
        assert response.status_code == 200, response.text
        return response.json()

    def get(self, path: str, agent: str = OWNER):
        return self.http.get(path, headers=self.admin_headers(agent))

    def health(self) -> dict:
        main._health_cache.clear()
        return self.http.get("/health").json()

    def backup(self) -> str:
        """Export through the API (create, then download) and return the file's text."""
        created = self.http.post("/system/backups", headers=self.admin_headers())
        assert created.status_code == 200, created.text
        downloaded = self.get(f"/system/backups/{created.json()['filename']}/download")
        assert downloaded.status_code == 200, downloaded.text
        return downloaded.text

    # --- the derived index ------------------------------------------------

    @staticmethod
    def later() -> None:
        """Ten minutes pass on the outbox's clock, so every retry backoff has run out."""
        Clock.offset += timedelta(minutes=10)

    def drain(self) -> int:
        """One pass of the processing worker's index step, after the retry backoff ran out."""
        self.later()
        return conversation_memory.index_pending(limit=100)

    def outbox(self) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(text("SELECT * FROM vector_outbox ORDER BY object_id")).mappings()
            return [dict(row) for row in rows]

    def stored_text(self) -> str:
        """Every value in every table, to prove a sentence survives nowhere."""
        found = []
        with self.engine.connect() as db:
            for table in inspect(self.engine).get_table_names():
                for row in db.execute(text(f'SELECT * FROM "{table}"')):
                    found.extend(str(value) for value in row)
        return "\n".join(found)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memorygate.db'}", connect_args={"check_same_thread": False}
    )
    # WAL, so a reader never blocks a writer: closer to Postgres for the concurrency tests.
    event.listen(
        engine, "connect", lambda connection, _: connection.execute("PRAGMA journal_mode=WAL")
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    original = core_db.SessionLocal
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] == "app" and getattr(module, "SessionLocal", None) is original:
            monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(core_db, "engine", engine)

    monkeypatch.setattr(auth_settings_service, "MEMORYGATE_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setattr(auth_settings_service, "_attempt_state", {})
    monkeypatch.setattr(auth_settings_service, "_PBKDF2_ROUNDS", 1000)  # speed, not semantics
    monkeypatch.setenv("MEMORYGATE_CORRECTION_KEY", CORRECTION_KEY)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_AGENT_ID", OWNER)
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_KEY", CONVERSATION_KEY)
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_AGENT_ID", OWNER)
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path / "backups"))

    index = VectorIndex()
    embeddings = {"up": True}

    def embedding_health():
        if embeddings["up"]:
            return {"status": "ok", "model": "word-buckets", "dimension": DIMENSION}
        return {"status": "unavailable", "reason": "embedding sidecar unavailable: ConnectError"}

    def embed_text(value):
        if not embeddings["up"]:
            raise EmbeddingUnavailable("embedding request failed: ConnectError")
        return embed(value)

    monkeypatch.setattr(qdrant_store, "get_qdrant_client", lambda: index)
    monkeypatch.setattr(qdrant_store, "embedding_health", embedding_health)
    monkeypatch.setattr(main, "embedding_health", embedding_health)
    monkeypatch.setattr(qdrant_store, "embed_text", embed_text)
    monkeypatch.setattr(qdrant_store, "EMBED_DIMENSION", DIMENSION)
    monkeypatch.setattr(vector_outbox, "datetime", Clock)
    monkeypatch.setattr(Clock, "offset", timedelta(0))
    qdrant_store.reset_ensured_collections()
    main._health_cache.clear()

    result = Stack(
        TestClient(main.app, raise_server_exceptions=False), sessions, engine, index, embeddings
    )
    with sessions() as db:
        auth_settings_service.ensure_bootstrap_agent_access_key(db, READ_KEY, OWNER)
        _, result.companion_key = auth_settings_service.create_agent_access_key(
            db, "companion", "companion"
        )
    yield result
    qdrant_store.reset_ensured_collections()
    main._health_cache.clear()
    index.client.close()
    engine.dispose()
