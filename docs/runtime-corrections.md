# Reviewed runtime text corrections

Pi can apply an owner-reviewed memory text correction without possessing a
MemoryGate administrator key. Configure two explicit server settings:

- `MEMORYGATE_CORRECTION_KEY`: a dedicated secret of at least 32 characters.
- `MEMORYGATE_CORRECTION_AGENT_ID`: the fixed allowed namespace, 1–128 ASCII
  letters/digits or `_.:-`, beginning with a letter or digit. There is no default.

Requests use `X-MemoryGate-Correction-Key`. Administrator, read and conversation
ingestion keys do not substitute for this capability. Missing/invalid configuration
fails closed. An optional `X-Agent-Id` must exactly match the configured namespace;
neither body nor query can select another namespace. Provision a distinct secret,
not an existing credential. Possession permits these corrections only; the service
does not claim that the key itself proves a human approved each request. Pi owns
the proposal/review boundary.

## Transport

`GET /runtime/corrections/memories/{memory_id}` returns authoritative bounded
`id`, `agent_id`, `revision`, `text`, `source_type`, and `confidence`.
Missing and foreign memories both return 404. Oversize historical text cannot be
silently truncated for review; it returns 409.

`PUT /runtime/corrections/{request_id}` accepts exactly:

```json
{"memory_id":"existing-memory-id","expected_revision":1,"text":"Owner-reviewed replacement"}
```

Text must be nonblank, contain no NUL, and have at most 16000 characters. Revision
is a positive integer, not a string or boolean. Memory IDs are 1–200 ASCII
letters/digits or `_.:-`, beginning with a letter or digit. Request IDs are
16–128 ASCII letters/digits, `_` or `-`. Unknown fields are rejected; validation
and storage errors never echo submitted text, credentials, or database errors.
The raw PUT body is bounded to 128000 bytes before JSON validation.

The receipt contains `request_id`, `memory_id`, `agent_id`, `previous_revision`,
`revision`, `status: "applied"`, and `indexing: "pending" | "indexed" | "degraded"`.
`GET /runtime/corrections/{request_id}` returns this durable receipt or 404. After
an uncertain transport result, reconcile by GET; do not invent another request ID.
Exact replay returns the same edit identity without repeating an edit, history,
audit, or indexing attempt. Different payload reuse returns 409. A stale revision
returns 409 without changing the memory.

The memory edit, revision increment, prior-state history, metadata-only audit and
receipt commit in one SQL transaction. ORM revision CAS protects concurrent ordinary
edits too. Equal replacement text still represents one reviewed revision. Receipts
do not store another copy of the replacement text and survive target deletion.

The only content mutation is `text` and a retrieval `summary` equal to that exact
text. Source type, confidence, memory type, status and other classifications remain
unchanged. No classifier, speculative confidence upgrade, conflict resolution or
general administrator action is invoked. Inferred evidence remains inferred.

Vector indexing uses the existing post-commit index operation, separately from the
authoritative edit. SQL text and summary are immediately authoritative. A pending
receipt means indexing might not have happened; degraded means indexing failed or
this revision was already superseded. Reconciliation/replay never reindexes. An
operator can use the existing index maintenance path; there is no hidden retry
worker. The indexing phase briefly locks the target SQL row, checks the committed
revision and performs the derived update before releasing it, preventing older
concurrent corrections from overwriting newer correction vectors. Index status
describes that attempt, not a guarantee about future memory changes.

The additive `memory_corrections` table is registered by the router import and
created through normal `Base.metadata.create_all` startup. No existing table or
credential migration is required.

Tests use isolated SQLite databases and stub the index boundary; they never call
providers, embeddings or Qdrant. From the repository root in PowerShell:

```powershell
$env:PYTHONPATH="$PWD/services/api"
$env:DATABASE_URL='sqlite:///:memory:'
& .venv/Scripts/python.exe -m pytest services/api/tests/test_memory_corrections.py services/api/tests/test_memory_revisions.py -q
```

PostgreSQL row locks use the same service code but are not exercised by the local
SQLite suite.
