"""Which model and provider answered, through the whole HTTP API.

Pi is a real HTTP server on a loopback port that answers like Pi's `POST /inference`. Evidence
goes in through `/runtime/ingest`, the real processing worker runs it, and what Pi named comes
back out of `/evidence/analysis`, `/runtime/ask` and `/runtime/health`. Only Pi's names are
stored: the direct path and older records say nothing, and a name Pi gives in an unusable shape
is dropped while its answer is kept.
"""

import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from app.services import ollama_service, pi_inference, processing_worker
from memory_stack import stack as stack

SERVICE_KEY = "pi-service-key-for-answered-by-tests"
EVIDENCE = "I always prefer to write code late at night"
# One JSON text that both model calls can read: the analysis keys and the Memory Lab answer.
ANSWER = json.dumps(
    {
        "summary": "Codes late at night",
        "observations": [],
        "memory_candidates": [{"text": "Prefers coding at night", "confidence": 0.7}],
        "answer": "Late at night.",
    }
)


class _Models(BaseHTTPRequestHandler):
    """Pi's `/inference`, and Ollama's `/api/chat` for the direct path."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.paths.append(self.path)
        if self.path == "/api/chat":
            status, reply = 200, {"model": "ollama-said-this", "message": {"content": ANSWER}}
        else:
            status, reply = self.server.reply
        encoded = json.dumps(reply).encode()
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


@pytest.fixture
def models(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Models)
    httpd.paths = []
    httpd.reply = (200, {"text": ANSWER, "model": "hosted-model-1", "provider": "hosted"})
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # A fresh process: nothing answered yet. Ollama's health probe finds nothing to ask.
    monkeypatch.setattr(pi_inference, "_last_answer", None)
    monkeypatch.setattr(ollama_service, "OLLAMA_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(processing_worker, "PROCESSING_POLL_SECONDS", 0.05)
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def pi(models, monkeypatch):
    monkeypatch.setattr(pi_inference, "PI_URL", models.url)
    monkeypatch.setattr(pi_inference, "PI_SERVICE_KEY", SERVICE_KEY)
    return models


def process(stack, content: str = EVIDENCE) -> dict:
    """Ingest one piece of evidence and let the real processing worker finish its job."""
    headers = stack.admin_headers()
    source = {"source_key": "notes", "source_type": "note", "label": "Notes"}
    assert stack.http.post("/evidence/sources", headers=headers, json=source).status_code == 200
    queued = stack.http.post(
        "/runtime/ingest", headers=headers, json={"source_key": "notes", "content": content}
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job_id"]
    processing_worker.start_worker()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            jobs = stack.get("/runtime/jobs").json()["results"]
            job = next(item for item in jobs if item["id"] == job_id)
            if job["status"] in {"completed", "failed"}:
                return job
            time.sleep(0.05)
    finally:
        processing_worker.stop_worker()
    pytest.fail("the processing job never finished")


def analyses(stack) -> dict[str, dict]:
    response = stack.get("/evidence/analysis")
    assert response.status_code == 200, response.text
    return {row["analysis_type"]: row for row in response.json()["results"]}


def ask(stack) -> dict:
    response = stack.http.post(
        "/runtime/ask", headers=stack.admin_headers(), json={"question": "When do I code?"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def model_choice(stack) -> dict:
    response = stack.get("/runtime/health")
    assert response.status_code == 200, response.text
    return response.json()["model_choice"]


# --- Pi names who answered ---------------------------------------------------------


def test_a_pi_proposal_stores_the_model_and_provider_that_wrote_it(stack, pi):
    assert process(stack)["status"] == "completed"

    stored = analyses(stack)
    proposal = stored["local_ai_proposal"]  # the stored type name stays as it was
    assert proposal["output_summary"] == "Codes late at night"
    assert (proposal["model"], proposal["provider"]) == ("hosted-model-1", "hosted")
    # The keyword analysis is not a model's work and names nobody.
    keywords = stored["deterministic_signal_extraction"]
    assert (keywords["model"], keywords["provider"]) == (None, None)
    assert "/api/chat" not in pi.paths


def test_memory_lab_answer_and_health_name_the_last_model(stack, pi):
    choice = model_choice(stack)
    assert choice["managed_by"] == "pi"
    assert choice["chosen_in"] == "Chosen in Conker: Settings, Models, Memory sorting"
    assert choice["last_answer"] is None  # nothing has answered yet

    pi.reply = (200, {"text": ANSWER, "model": "hosted-model-2", "provider": "other"})
    answered = ask(stack)
    assert answered["answer"] == "Late at night."
    assert (answered["model"], answered["provider"]) == ("hosted-model-2", "other")

    last = model_choice(stack)["last_answer"]
    assert (last["model"], last["provider"]) == ("hosted-model-2", "other")
    assert last["at"]


def test_after_a_restart_health_names_the_newest_stored_proposal(stack, pi, monkeypatch):
    process(stack)
    monkeypatch.setattr(pi_inference, "_last_answer", None)  # MemoryGate restarted

    last = model_choice(stack)["last_answer"]
    assert (last["model"], last["provider"]) == ("hosted-model-1", "hosted")
    assert last["at"]


@pytest.mark.parametrize(
    ("named", "stored"),
    [
        ({}, (None, None)),
        ({"model": 7, "provider": ["hosted"]}, (None, None)),
        ({"model": "  ", "provider": ""}, (None, None)),
        ({"model": "m" * 201, "provider": "hosted"}, (None, "hosted")),
        ({"model": "bad\nname", "provider": "bad\x00name"}, (None, None)),
        ({"model": " hosted-model-3 "}, ("hosted-model-3", None)),
    ],
)
def test_unusable_names_are_dropped_and_the_answer_is_kept(stack, pi, named, stored):
    pi.reply = (200, {"text": ANSWER, **named})
    process(stack)
    answered = ask(stack)

    proposal = analyses(stack)["local_ai_proposal"]
    assert proposal["output_summary"] == "Codes late at night"
    assert (proposal["model"], proposal["provider"]) == stored
    assert answered["answer"] == "Late at night."
    assert (answered["model"], answered["provider"]) == stored
    last = model_choice(stack)["last_answer"]
    if stored[0] is None:
        assert last is None  # an answer without a model name is not "the last model"
    else:
        assert (last["model"], last["provider"]) == stored


# --- No name to give ----------------------------------------------------------------


def test_pi_without_an_answer_stores_a_proposal_that_names_nobody(stack, pi):
    pi.reply = (503, {"detail": {"code": "no_model", "message": "No model for this role"}})
    assert process(stack)["status"] == "completed"

    proposal = analyses(stack)["local_ai_proposal"]
    assert proposal["output_summary"] == ""
    assert (proposal["model"], proposal["provider"]) == (None, None)
    assert model_choice(stack)["last_answer"] is None


def test_the_direct_path_names_nobody_and_health_keeps_its_shape(stack, models, monkeypatch):
    monkeypatch.setattr(pi_inference, "PI_URL", "")
    monkeypatch.setattr(pi_inference, "PI_SERVICE_KEY", "")
    monkeypatch.setattr(ollama_service, "OLLAMA_ENABLED", True)
    monkeypatch.setattr(ollama_service, "OLLAMA_URL", models.url)

    process(stack)
    answered = ask(stack)

    assert "/inference" not in models.paths and "/api/chat" in models.paths
    proposal = analyses(stack)["local_ai_proposal"]
    assert proposal["output_summary"] == "Codes late at night"
    assert (proposal["model"], proposal["provider"]) == (None, None)
    assert answered["answer"] == "Late at night."
    assert (answered["model"], answered["provider"]) == (None, None)
    assert model_choice(stack) == {"managed_by": "memorygate", "editable": True}
