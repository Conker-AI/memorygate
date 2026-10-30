# Developing MemoryGate

## Development

### API

```powershell
cd services/api
docker build -t memorygate-api:local .
```

### Dashboard

```powershell
cd dashboard
npm ci
npm run build
```

### Tests

```bash
cd services/api
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests
```

On Windows, drop `uvloop` from the install: it has no Windows build. The suite needs no running
services - it uses a real SQLite database and points its dependency probes at a closed port so the
degraded paths are exercised for real rather than mocked.

Tests keep their files in pytest's `tmp_path`, never in the checkout. `services/api/tests/conftest.py`
(and `integrations/conftest.py`) fail any test that writes into the repository, naming the path, and
refuse a `--basetemp` or temporary directory inside it. The guard lives in
`services/api/tests/repository_guard.py`. The mutation drill keeps its copies in the OS temp directory.

### Verification

```powershell
docker ps
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8020/health
```

`GET /health` is unauthenticated and runs real probes against PostgreSQL, Qdrant, and the embedding
provider. It reports `ok` only when all three answer, and otherwise `degraded` with each failing
dependency named. Probe detail is deliberately coarse, since the route has no auth. Results are
cached for five seconds and carry their `age_seconds`.

## Project Layout

```text
dashboard/             React administrative console
services/api/          FastAPI service, models, retrieval, workers, and security
services/cli/          Terminal client for agent integrations
services/mcp/          MCP server bridge
integrations/          Read-only agent skill and MCP configuration
```
