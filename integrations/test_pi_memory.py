"""Pi -> HTTP contract -> MemoryGate SQL -> later Pi turn, in both languages.

Run explicitly with both checkouts on PYTHONPATH. The default drill exercises
honest lexical degradation; MEMORY_E2E_SEMANTIC=1 requires live embeddings/Qdrant
and fails rather than skips if semantic retrieval cannot prove the same result.
"""

import json
import os

import pytest
from app.core import auth
from app.core.db import Base
from app.models.memory import Memory as MemoryRow
from app.routes import conversation, runtime
from app.services import conversation_memory, embeddings, qdrant_store
from app.services.auth_settings_service import create_agent_access_key
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pi.conversation import forgetting
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory.service import Memory, MemoryClient
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.turns.loop import Loop
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


class Recorder:
    name = "recorder"

    def __init__(self):
        self.messages = []

    def complete(self, messages, *, model):
        self.messages = messages
        return Completion(text="Recorded reply", provider=self.name, model=model)


CASES = [
    ("I prefer to train before school", "Help me plan training before school"),
    ("Я предпочитаю тренироваться перед школой", "Когда лучше тренироваться перед школой?"),
]
if os.environ.get("MEMORY_E2E_SEMANTIC") == "1":
    CASES += [(CASES[0][0], CASES[1][1]), (CASES[1][0], CASES[0][1])]


@pytest.mark.parametrize("statement,query", CASES)
def test_remembers_after_restart_then_forgets_source_and_cached_context(
    tmp_path, monkeypatch, statement, query
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memorygate.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    for module in (auth, runtime, conversation_memory):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_KEY", "test-ingestion-credential")
    monkeypatch.setenv("MEMORYGATE_CONVERSATION_AGENT_ID", "owner")
    semantic = os.environ.get("MEMORY_E2E_SEMANTIC") == "1"
    local_index = None
    if os.environ.get("MEMORY_E2E_LOCAL_INDEX") == "1":
        from qdrant_client import QdrantClient

        assert semantic, "Embedded index mode still requires real semantic vectors"
        local_index = QdrantClient(path=str(tmp_path / "qdrant"))
        monkeypatch.setattr(qdrant_store, "get_qdrant_client", lambda: local_index)
        qdrant_store.reset_ensured_collections()
    if not semantic:
        monkeypatch.setattr(embeddings, "EMBEDDINGS_KEY", "")
    embeddings.reset_provider_cache()
    with sessions() as db:
        _, read_key = create_agent_access_key(db, "Pi drill", "owner")
    remote = FastAPI()
    remote.include_router(conversation.router)
    remote.include_router(runtime.router)

    def connect(store):
        client = MemoryClient("http://testserver", "test-ingestion-credential", read_key, "owner")
        client.http.close()
        # Real route handlers, authentication and databases; only the socket is
        # replaced by ASGI transport, and the language model by a recorder.
        client.http = TestClient(remote)
        return Memory(store, client)

    path = tmp_path / "pi.db"
    store = Store(path)
    source_session = store.create_session()
    memory = connect(store)
    loop = Loop(store, Router(local_provider=Recorder(), local_model="test"), memory=memory)
    loop.run_turn(source_session, statement)
    message_id = store.messages(source_session)[0]["id"]
    assert memory.drain_once() == 1
    # Simulate receiver commit followed by a lost local acknowledgment.
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET state='pending'")
    memory.close()
    store.close()
    store = Store(path)
    memory = connect(store)
    assert memory.drain_once() == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(MemoryRow)) == 1
    if semantic:
        distractor = store.create_session()
        store.append_message(distractor, "user", "I prefer reading science fiction before bed")
        assert memory.drain_once() == 1
        assert conversation_memory.index_pending() == 2
    provider = Recorder()
    loop = Loop(store, Router(local_provider=provider, local_model="test"), memory=memory)
    other = store.create_session()
    recalled = loop.run_turn(other, query)
    package = recalled["memory"]["retrieval"]["package"]
    assert package is not None
    found = next(row for row in package["memories"] if row["text"] == statement)
    if semantic:
        assert found["retrieval_path"] == "semantic"
        assert package["memories"][0]["id"] == found["id"]
    assert found["citations"][0]["message_id"] == message_id
    assert statement in provider.messages[0].content
    assert json.dumps(package, ensure_ascii=False) in provider.messages[0].content
    assert recalled["memory"]["retrieval"]["status"] == ("ok" if semantic else "degraded")
    memory.close()
    store.close()
    plan = forgetting.preview(path, source_session)
    forgetting.forget(path, source_session, plan["confirmation"])
    store = Store(path)
    memory = connect(store)
    assert memory_store.context(store, recalled["turn_id"])["package"] is None
    assert memory.drain_once() >= 1
    assert store.get_message(message_id)["content_status"] == "forgotten"
    package = memory.client.retrieve(query)
    assert statement not in json.dumps(package, ensure_ascii=False)
    with sessions() as db:
        assert not db.scalars(select(MemoryRow).where(MemoryRow.text == statement)).all()
    if semantic:
        assert conversation_memory.index_pending() >= 1
    memory.close()
    store.close()
    engine.dispose()
    embeddings.reset_provider_cache()
    if local_index:
        local_index.close()
        qdrant_store.reset_ensured_collections()
