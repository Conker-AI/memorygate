"""Model calls through Pi's `memory-sorting` role, and the direct path when Pi is not set up.

Pi is a real HTTP server on a loopback port, so requests, refusals and timeouts happen for real.
"""

import contextlib
import json
import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from app.core import auth
from app.core.db import Base
from app.models.ai_runtime_setting import AiRuntimeSetting
from app.models.analysis_object import AnalysisObject
from app.models.evidence_object import EvidenceObject
from app.models.processing_job import ProcessingJob
from app.routes import runtime as runtime_routes
from app.routes import system as system_routes
from app.services import ollama_service, pi_inference, runtime_pipeline
from app.services.ai_runtime_service import update_runtime_config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SERVICE_KEY = "pi-service-key-for-tests-only-7f3a9c"
EVIDENCE = "I always prefer to write code late at night"
CONTEXT = {"memories": [{"text": "Prefers tea over coffee"}]}
CONTRACT_FIELDS = {"role", "system", "prompt", "max_tokens", "json", "request_id"}


class _FakePi(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.calls.append(
            {"path": self.path, "key": self.headers.get("X-Pi-Service-Key"), "body": body}
        )
        status, reply, delay = self.server.reply
        if self.path == "/api/chat":
            status, reply, delay = 200, {"message": {"content": '{"answer":"direct"}'}}, 0
        time.sleep(delay)
        encoded = json.dumps(reply).encode()
        # A timed-out caller has already hung up; the late answer has nowhere to go.
        with contextlib.suppress(ConnectionError):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _NoDb:
    def close(self):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakePi)
    httpd.calls = []
    httpd.reply = (200, {"text": "{}", "model": "test-model", "provider": "test"}, 0)
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def pi(server, monkeypatch):
    """Pi configured. MemoryGate's own runtime setting and Ollama must not be touched."""
    monkeypatch.setattr(pi_inference, "PI_URL", server.url)
    monkeypatch.setattr(pi_inference, "PI_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setattr(ollama_service, "OLLAMA_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(
        ollama_service, "SessionLocal", lambda: pytest.fail("direct path used with Pi set up")
    )
    return server


def _direct_path(monkeypatch, *, enabled: bool, url: str = "") -> None:
    monkeypatch.setattr(ollama_service, "OLLAMA_ENABLED", enabled)
    monkeypatch.setattr(ollama_service, "OLLAMA_URL", url or f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(ollama_service, "SessionLocal", _NoDb)
    monkeypatch.setattr(
        ollama_service,
        "get_runtime_config",
        lambda db: {"provider": "ollama", "model": "test-model", "api_key": ""},
    )


# --- Pi chooses the model --------------------------------------------------------


def test_every_model_call_goes_to_pi_memory_sorting(pi):
    pi.reply = (
        200,
        {
            "text": json.dumps(
                {
                    "summary": "Codes at night",
                    "observations": [],
                    "memory_candidates": [{"text": "Prefers coding at night", "confidence": 0.7}],
                }
            ),
            "model": "hosted-test",
            "provider": "test",
        },
        0,
    )
    analysis = ollama_service.analyze_evidence(EVIDENCE, "listener")
    pi.reply = (200, {"text": '{"answer": "Tea."}', "model": "hosted-test", "provider": "test"}, 0)
    answer = ollama_service.answer_with_context("What do I drink?", CONTEXT)

    assert analysis["summary"] == "Codes at night"
    assert analysis["memory_candidates"][0]["text"] == "Prefers coding at night"
    assert answer == "Tea."
    assert [call["path"] for call in pi.calls] == ["/inference", "/inference"]
    first, second = (call["body"] for call in pi.calls)
    for call in pi.calls:
        assert call["key"] == SERVICE_KEY
        assert set(call["body"]) == CONTRACT_FIELDS
        assert call["body"]["role"] == "memory-sorting"
        assert call["body"]["json"] is True
        assert re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", call["body"]["request_id"])
    assert first["system"] == ollama_service.SYSTEM_PROMPT
    assert EVIDENCE in first["prompt"] and first["max_tokens"] == 160
    assert "Prefers tea over coffee" in second["prompt"] and second["max_tokens"] == 220
    assert first["request_id"] != second["request_id"]


def test_pi_path_skips_the_stored_hosted_setting(pi, monkeypatch):
    """The stored setting decides nothing, including its hosted-budget refusal."""
    monkeypatch.setattr(
        ollama_service, "record_refusal", lambda *_a: pytest.fail("stored setting consulted")
    )
    assert ollama_service._generate("system", "prompt", 10) == "{}"


# --- Pi has no answer: exactly today's "model unavailable" fallback ---------------


def _calls():
    return (
        ollama_service._generate("system", "prompt", 160),
        ollama_service.analyze_evidence(EVIDENCE, "listener"),
        ollama_service.answer_with_context("What do I drink?", CONTEXT),
    )


def test_pi_503_falls_back_exactly_like_an_unavailable_local_model(pi, monkeypatch):
    pi.reply = (503, {"detail": {"code": "no_model", "message": "No model for this role"}}, 0)
    via_pi = _calls()
    assert len(pi.calls) == 3

    monkeypatch.setattr(pi_inference, "PI_URL", "")
    _direct_path(monkeypatch, enabled=False)
    today = _calls()

    assert via_pi == today
    generated, analysis, answer = via_pi
    assert generated is None and answer is None
    # Nothing is made up: no summary, no observations, no candidates.
    assert analysis == {"summary": "", "observations": [], "memory_candidates": []}


@pytest.mark.parametrize(
    ("status", "reply"),
    [
        (503, {"detail": {"code": "unavailable", "message": "provider failed"}}),
        (401, {"detail": "missing or invalid service key"}),
        (403, {"detail": "role not allowed"}),
        (422, {"detail": [{"msg": "bad body"}]}),
        (200, {"model": "hosted-test", "provider": "test"}),
        (200, ["not", "an", "object"]),
    ],
)
def test_any_pi_refusal_or_bad_answer_means_no_model_answer(pi, status, reply):
    pi.reply = (status, reply, 0)
    assert ollama_service._generate("system", "prompt", 160) is None


def test_pi_timeout_is_bounded_and_means_no_model_answer(pi, monkeypatch, caplog):
    assert pi_inference.TIMEOUT.read is not None and pi_inference.TIMEOUT.connect is not None
    monkeypatch.setattr(pi_inference, "TIMEOUT", httpx.Timeout(0.3, connect=0.3))
    pi.reply = (200, {"text": '{"answer": "late"}', "model": "m", "provider": "p"}, 3)
    assert ollama_service._generate("system", "prompt", 160) is None
    assert "Pi inference timed out" in caplog.text


def test_unreachable_pi_means_no_model_answer(pi, monkeypatch):
    monkeypatch.setattr(pi_inference, "PI_URL", f"http://127.0.0.1:{_closed_port()}")
    assert ollama_service._generate("system", "prompt", 160) is None


def test_processing_completes_on_pi_503_without_invented_memory(pi, tmp_path):
    pi.reply = (503, {"detail": {"code": "unavailable", "message": "provider failed"}}, 0)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pi.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        with sessions() as db:
            evidence = EvidenceObject(
                agent_id="owner",
                source_id="listener",
                source_key="listener",
                source_type="listener",
                summary=EVIDENCE,
            )
            db.add(evidence)
            db.commit()
            result = runtime_pipeline.process_evidence(db, evidence, EVIDENCE)
            proposal = db.get(AnalysisObject, result["ai_analysis_id"])
            job = db.query(ProcessingJob).one()
            assert job.status == "completed"
            assert proposal.output_summary == ""
            assert json.loads(proposal.steps_json) == {"observations": [], "memory_candidates": []}
    finally:
        engine.dispose()


# --- Pi not set up: today's direct path ------------------------------------------


@pytest.mark.parametrize(
    ("url", "key"),
    [("", ""), ("server", ""), ("", SERVICE_KEY), ("pi-api:8000", SERVICE_KEY)],
)
def test_without_both_pi_settings_the_direct_path_stays(server, monkeypatch, url, key):
    monkeypatch.setattr(pi_inference, "PI_URL", server.url if url == "server" else url)
    monkeypatch.setattr(pi_inference, "PI_SERVICE_KEY", key)
    _direct_path(monkeypatch, enabled=True, url=server.url)
    assert pi_inference.model_choice() == {"managed_by": "memorygate", "editable": True}
    assert ollama_service._generate("system", "prompt", 10) == '{"answer":"direct"}'
    assert [call["path"] for call in server.calls] == ["/api/chat"]
    assert all(call["key"] is None for call in server.calls)


# --- Settings and runtime routes -------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'routes.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False, autocommit=False)
    for module in (system_routes, runtime_routes):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    with sessions() as db:
        update_runtime_config(db, "ollama", "qwen3:4b", None)
    app = FastAPI()
    app.include_router(system_routes.router)
    app.include_router(runtime_routes.router)
    app.dependency_overrides[auth.require_key] = lambda: "admin"
    app.dependency_overrides[auth.require_read_key] = lambda: "admin"
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.sessions = sessions
    yield test_client
    engine.dispose()


def test_with_pi_the_model_setting_is_read_only_and_says_where_it_is_chosen(pi, client):
    status = client.get("/system/ai-runtime")
    assert status.status_code == 200
    assert status.json() == {
        "managed_by": "pi",
        "editable": False,
        "role": "memory-sorting",
        "chosen_in": "Chosen in Conker: Settings, Models, Memory sorting",
        "provider": "pi",
        "generation_status": "pi",
        "ollama": None,
    }

    refused = client.put(
        "/system/ai-runtime",
        json={
            "current_key": "anything",
            "provider": "ollama",
            "model": "other-model",
        },
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == pi_inference.CHOSEN_IN
    with client.sessions() as db:
        stored = db.get(AiRuntimeSetting, "singleton")
        assert (stored.provider, stored.model) == ("ollama", "qwen3:4b")

    health = client.get("/runtime/health").json()
    assert health["model_choice"]["chosen_in"] == pi_inference.CHOSEN_IN


def test_with_pi_unavailable_ask_names_where_the_model_is_chosen(pi, client, monkeypatch):
    pi.reply = (503, {"detail": {"code": "no_model", "message": "No model for this role"}}, 0)
    monkeypatch.setattr(runtime_routes, "_build_context", lambda *_a: CONTEXT)
    refused = client.post("/runtime/ask", json={"question": "What do I drink?"})
    assert refused.status_code == 503
    assert pi_inference.CHOSEN_IN in refused.json()["detail"]


def test_without_pi_the_stored_setting_still_shows_and_decides(client, monkeypatch):
    monkeypatch.setattr(pi_inference, "PI_URL", "")
    monkeypatch.setattr(pi_inference, "PI_SERVICE_KEY", "")
    monkeypatch.setattr(ollama_service, "OLLAMA_URL", f"http://127.0.0.1:{_closed_port()}")
    status = client.get("/system/ai-runtime").json()
    assert status["managed_by"] == "memorygate" and status["editable"] is True
    assert (status["provider"], status["model"]) == ("ollama", "qwen3:4b")
    assert status["ollama"]["available"] is False


# --- The service key stays secret -------------------------------------------------


def test_service_key_never_reaches_logs_or_responses(pi, client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(pi_inference, "TIMEOUT", httpx.Timeout(0.3, connect=0.3))
    monkeypatch.setattr(runtime_routes, "_build_context", lambda *_a: CONTEXT)
    bodies = []
    for reply in [
        (200, {"text": '{"answer": "Tea."}', "model": "m", "provider": "p"}, 0),
        (503, {"detail": {"code": "unavailable", "message": SERVICE_KEY}}, 0),
        (401, {"detail": "missing or invalid service key"}, 0),
        (200, {"text": "{}", "model": "m", "provider": "p"}, 3),
    ]:
        pi.reply = reply
        bodies.append(client.post("/runtime/ask", json={"question": "What do I drink?"}).text)
    bodies.append(client.get("/system/ai-runtime").text)
    bodies.append(client.get("/runtime/health").text)
    bodies.append(
        client.put(
            "/system/ai-runtime",
            json={"current_key": "anything", "provider": "ollama", "model": "m"},
        ).text
    )

    assert "Pi inference" in caplog.text
    assert SERVICE_KEY not in caplog.text
    assert "Prefers tea over coffee" not in caplog.text
    assert all(SERVICE_KEY not in body for body in bodies)
