"""Without Pi, health reports the model MemoryGate's own calls send, not the env default."""

from app.core import auth
from app.core.db import Base
from app.routes import runtime as runtime_routes
from app.routes import system as system_routes
from app.services import ollama_service, pi_inference
from app.services.ai_runtime_service import update_runtime_config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _Tags:
    """httpx.Client stand-in: Ollama answering /api/tags with `names`."""

    def __init__(self, names):
        self.names = names

    def __call__(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, _url):
        return self

    def raise_for_status(self):
        return None

    def json(self):
        return {"models": [{"name": name} for name in self.names]}


def test_health_checks_the_model_the_owner_chose(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'h.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False)
    for module in (system_routes, runtime_routes):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(pi_inference, "PI_URL", "")
    monkeypatch.setattr(pi_inference, "PI_SERVICE_KEY", "")
    monkeypatch.setattr(ollama_service, "OLLAMA_MODEL", "env-default:3b")
    with sessions() as db:
        update_runtime_config(db, "ollama", "chosen:7b", None)
    app = FastAPI()
    app.include_router(system_routes.router)
    app.include_router(runtime_routes.router)
    app.dependency_overrides[auth.require_key] = lambda: "admin"
    client = TestClient(app, raise_server_exceptions=False)

    monkeypatch.setattr(ollama_service.httpx, "Client", _Tags(["chosen:7b"]))
    for path in ("/system/ai-runtime", "/runtime/health"):
        health = client.get(path).json()["ollama"]
        assert (health["model"], health["model_ready"]) == ("chosen:7b", True), path

    monkeypatch.setattr(ollama_service.httpx, "Client", _Tags(["env-default:3b"]))
    for path in ("/system/ai-runtime", "/runtime/health"):
        health = client.get(path).json()["ollama"]
        assert (health["model"], health["model_ready"]) == ("chosen:7b", False), path
    engine.dispose()
