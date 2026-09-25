import json
import time

import pytest
from app.core.db import Base
from app.models.audit import MemoryAudit
from app.services import ollama_service
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(ollama_service, "SessionLocal", factory)
    monkeypatch.setattr(ollama_service, "get_runtime_config", lambda db: {
        "provider": "openai", "model": "hosted-test", "api_key": "never-send-this"})
    monkeypatch.delenv("MEMORYGATE_HOSTED_COST_QUOTE", raising=False)
    yield factory
    engine.dispose()


def test_unbudgeted_hosted_generation_never_dispatches_and_records_unknown_cost(sessions, monkeypatch):
    monkeypatch.setattr(ollama_service.httpx, "Client", lambda **kw: pytest.fail("unbudgeted hosted dispatch"))
    assert ollama_service._generate("system", "private-evidence", 160) is None
    with sessions() as db:
        decision = json.loads(db.scalars(select(MemoryAudit)).one().payload_json)
    assert decision["dispatched"] is False
    assert decision["reason"] == "HOSTED_BUDGET_UNAVAILABLE"
    assert decision["quote"]["upper_bound_microusd"] is None
    assert "private-evidence" not in json.dumps(decision)
    assert "never-send-this" not in json.dumps(decision)


def test_cost_quote_is_recorded_but_does_not_authorize_spending(sessions, monkeypatch):
    monkeypatch.setenv("MEMORYGATE_HOSTED_COST_QUOTE", json.dumps({
        "model": "hosted-test", "valid_until": time.time()+60, "source": "https://provider.example/prices",
        "input_token_ceiling": 1000, "input_per_million_microusd": 1000000,
        "output_per_million_microusd": 2000000, "status": "free", "upper_bound_microusd": 0}))
    monkeypatch.setattr(ollama_service.httpx, "Client", lambda **kw: pytest.fail("estimate is not authorization"))
    assert ollama_service._generate("system", "private", 160) is None
    with sessions() as db:
        decision = json.loads(db.scalars(select(MemoryAudit)).one().payload_json)
    assert decision["quote"]["upper_bound_microusd"] == 1320
    assert decision["quote"]["status"] == "owner_supplied_estimate"


@pytest.mark.parametrize("mode", ["exception", "unknown_dimension"])
def test_failed_collection_inspection_is_never_healthy(monkeypatch, mode):
    from types import SimpleNamespace as NS

    from app.services import qdrant_store as store

    class Client:
        def get_collections(self):
            return NS(collections=[NS(name=store.QDRANT_COLLECTION)])
        def get_collection(self, name):
            if mode == "exception":
                raise RuntimeError("collection inspection unavailable")
            return NS(config=NS(params=NS(vectors=NS(size=None))))

    monkeypatch.setattr(store, "get_qdrant_client", Client)
    report = store.qdrant_health()
    assert report["status"] == "degraded"
    assert store.QDRANT_COLLECTION in report["reason"]
    assert ("RuntimeError" if mode == "exception" else "unknown vector dimension") in report["reason"]


def test_oversized_ingestion_is_permanent_without_echoing_content(monkeypatch):
    from app.routes import conversation
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MEMORYGATE_CONVERSATION_KEY", "test-conversation-secret")
    calls = []
    monkeypatch.setattr(conversation.conversation_memory, "ingest", lambda *args: calls.append(args) or {"status": "accepted"})
    app = FastAPI()
    app.include_router(conversation.router)
    client = TestClient(app)
    body = {"session_id": "ses_one", "content": "x"*16001, "created_at": 1000}
    response = client.put("/runtime/conversation/msg_one", json=body,
                          headers={"X-MemoryGate-Conversation-Key": "test-conversation-secret"})
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "CONTENT_TOO_LARGE"
    assert response.json()["detail"]["retryable"] is False
    assert response.json()["detail"]["max_content_characters"] == 16000
    assert "x"*100 not in response.text
    assert calls == []
    body["content"] = "x"*16000
    assert client.put("/runtime/conversation/msg_one", json=body,
                      headers={"X-MemoryGate-Conversation-Key": "test-conversation-secret"}).status_code == 200
    assert len(calls) == 1
    client.close()


def test_cryptography_runtime_and_manifest_cover_both_advisories():
    from importlib.metadata import version
    from pathlib import Path

    from cryptography.fernet import Fernet, InvalidToken
    from packaging.version import Version

    requirements = Path(__file__).parents[1] / "requirements.txt"
    pin = next(line.split("==")[1] for line in requirements.read_text().splitlines()
               if line.startswith("cryptography=="))
    assert Version(pin) >= Version("50.0.0")
    assert Version(version("cryptography")) >= Version("50.0.0")
    # The published format vector predates this upgrade; stored Fernet tokens must remain readable.
    cipher = Fernet(b"cw_0x689RpI-jtRR7oE8h_eQsKImvJapLeSbXpwF4e4=")
    token = b"gAAAAAAdwJ6wAAECAwQFBgcICQoLDA0ODy021cpGVWKZ_eEwCGM4BLLF_5CV9dOPmrhuVUPgJobwOz7JcbmrR64jVmpU4IwqDA=="
    assert cipher.decrypt(token) == b"hello"
    with pytest.raises(InvalidToken):
        cipher.decrypt(token[:-10] + b"tampered==")
