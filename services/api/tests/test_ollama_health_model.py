"""Health reports the model MemoryGate actually sends, not the env default."""

from app.services import ollama_service


class _Tags:
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


def test_health_checks_the_model_the_owner_chose(monkeypatch):
    monkeypatch.setattr(ollama_service, "get_runtime_config",
                        lambda db: {"provider": "ollama", "model": "chosen:7b", "api_key": ""})
    monkeypatch.setattr(ollama_service.httpx, "Client", _Tags(["chosen:7b"]))
    assert ollama_service.ollama_health()["model"] == "chosen:7b"
    assert ollama_service.ollama_health()["model_ready"] is True

    monkeypatch.setattr(ollama_service.httpx, "Client", _Tags(["qwen2.5:3b"]))
    health = ollama_service.ollama_health()
    assert health["model"] == "chosen:7b" and health["model_ready"] is False
