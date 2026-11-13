"""Outbound HTTP must ignore ambient proxies and never follow redirects."""

import ast
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError

import pytest
from app.services import embeddings, ollama_service, qdrant_store
from qdrant_client.http.exceptions import UnexpectedResponse

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOTS = (
    REPO_ROOT / "services" / "api" / "app",
    REPO_ROOT / "services" / "cli",
    REPO_ROOT / "services" / "mcp",
)


class _RecordingHandler(BaseHTTPRequestHandler):
    server_version = "MemoryGateEgressTest/1"

    def _record(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        self.server.hits.append((self.command, self.path))

    def _send_json(self, body: dict, status: int = 200) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _respond(self) -> None:
        self._record()
        if self.server.redirect_base is not None:
            self.send_response(302)
            self.send_header("Location", f"{self.server.redirect_base}/redirected")
            self.end_headers()
            return
        if self.path.endswith("/model"):
            self._send_json({"model": "test-embedding", "dimension": 2})
        elif self.path.endswith("/embed"):
            self._send_json({"vectors": [[0.25, 0.75]]})
        elif self.path.endswith("/api/tags"):
            self._send_json({"models": [{"name": "test-ollama"}]})
        elif self.path.endswith("/api/chat"):
            self._send_json({"message": {"content": '{"answer":"ok"}'}})
        elif self.path.endswith("/collections"):
            self._send_json({"result": {"collections": []}, "status": "ok", "time": 0})
        else:
            self._send_json({"status": "ok"})

    do_GET = _respond
    do_POST = _respond

    def log_message(self, format, *args):
        return


@pytest.fixture
def http_servers():
    servers = []

    def start(*, redirect_base=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        server.hits = []
        server.redirect_base = redirect_base
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server, f"http://127.0.0.1:{server.server_port}"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _load_script(relative_path: str) -> ModuleType:
    path = REPO_ROOT / relative_path
    name = "_egress_test_" + "_".join(path.parts[-3:]).replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_hostile_proxy(monkeypatch, proxy_url: str) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def _configure_api_clients(monkeypatch, target_url: str) -> None:
    monkeypatch.setattr(embeddings, "EMBEDDINGS_URL", target_url)
    monkeypatch.setattr(embeddings, "EMBEDDINGS_KEY", "test-key")
    embeddings.reset_provider_cache()
    monkeypatch.setattr(ollama_service, "OLLAMA_URL", target_url)
    monkeypatch.setattr(ollama_service, "OLLAMA_MODEL", "test-ollama")
    monkeypatch.setattr(ollama_service, "OLLAMA_ENABLED", True)
    monkeypatch.setattr(ollama_service, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(
        ollama_service,
        "get_runtime_config",
        lambda db: {"provider": "ollama", "model": "test-ollama"},
    )
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", target_url)
    qdrant_store.get_qdrant_client.cache_clear()


def test_every_outbound_client_bypasses_hostile_ambient_proxy(
    monkeypatch, http_servers
):
    proxy, proxy_url = http_servers()
    target, target_url = http_servers()
    _set_hostile_proxy(monkeypatch, proxy_url)
    _configure_api_clients(monkeypatch, target_url)
    monkeypatch.setenv("MEMORYGATE_URL", target_url)
    monkeypatch.setenv("MEMORYGATE_KEY", "test-key")

    assert embeddings.embedding_health()["status"] == "ok"
    assert embeddings.embed_text("hello") == [0.25, 0.75]
    assert ollama_service.ollama_health()["available"] is True
    assert ollama_service._generate("system", "prompt", 10) == '{"answer":"ok"}'
    assert qdrant_store.get_qdrant_client().get_collections().collections == []
    assert _load_script("services/cli/memorygate.py").request("GET", "/health") == {
        "status": "ok"
    }
    assert _load_script("services/mcp/memorygate_mcp.py").exchange("context", {}) == {
        "status": "ok"
    }

    assert proxy.hits == []
    assert len(target.hits) == 7


def test_every_transport_refuses_redirects(monkeypatch, http_servers):
    redirect_sink, sink_url = http_servers()
    target, target_url = http_servers(redirect_base=sink_url)
    _configure_api_clients(monkeypatch, target_url)
    monkeypatch.setenv("MEMORYGATE_URL", target_url)
    monkeypatch.setenv("MEMORYGATE_KEY", "test-key")

    assert embeddings.embedding_health()["status"] == "unavailable"
    assert ollama_service.ollama_health()["available"] is False
    with pytest.raises(UnexpectedResponse):
        qdrant_store.get_qdrant_client().get_collections()
    with pytest.raises(HTTPError) as cli_error:
        _load_script("services/cli/memorygate.py").request("GET", "/health")
    with pytest.raises(HTTPError) as mcp_error:
        _load_script("services/mcp/memorygate_mcp.py").exchange("context", {})

    assert cli_error.value.code == 302
    assert mcp_error.value.code == 302
    assert redirect_sink.hits == []
    assert len(target.hits) == 5


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return f"{node.func.value.id}.{node.func.attr}"
    return ""


def _has_false_keyword(node: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in node.keywords
    )


def test_outbound_http_static_policy():
    violations = []
    for root in PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(REPO_ROOT)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "urllib.request"
                    and any(alias.name == "urlopen" for alias in node.names)
                ):
                    violations.append(f"{relative}:{node.lineno}: urlopen inherits ambient proxies")
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name in {
                    "httpx.get",
                    "httpx.post",
                    "httpx.put",
                    "httpx.patch",
                    "httpx.delete",
                    "httpx.request",
                    "httpx.Client",
                    "httpx.AsyncClient",
                }:
                    for keyword in ("trust_env", "follow_redirects"):
                        if not _has_false_keyword(node, keyword):
                            violations.append(
                                f"{relative}:{node.lineno}: {name} needs {keyword}=False"
                            )
                if name == "QdrantClient":
                    for keyword in ("trust_env", "follow_redirects"):
                        if not _has_false_keyword(node, keyword):
                            violations.append(
                                f"{relative}:{node.lineno}: QdrantClient needs {keyword}=False"
                            )

            source = path.read_text(encoding="utf-8")
            if "build_opener(" in source and (
                "ProxyHandler({})" not in source or "_NoRedirectHandler()" not in source
            ):
                violations.append(f"{relative}: urllib opener must disable proxies and redirects")

    assert violations == []
