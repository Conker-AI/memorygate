# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry - replacing a module has to be
a decision with visible consequences.

## Unreleased

- Search and listener dedupe passed query text into SQL `LIKE` unescaped, so `%` and `_` acted as
  wildcards: searching for `%` matched every memory and entity, and a listener event "100% done"
  deduplicated against a stored "100 is done" (linking its evidence to the wrong memory, and two
  stored memories with the same text failed the job outright). Query text is now matched literally,
  and the dedupe picks the first of several equal texts instead of failing.
- A read key's failed attempts are counted per known agent, and every agent id with no key
  shares one count per client address. The count used to be per `X-Agent-Id` header, which
  the caller writes: a new made-up id on each try earned five fresh attempts and added an entry
  to the in-memory lockout table, so guessing never locked out and the table grew without
  bound. Now the fifth wrong key from one address answers 429 whatever id it sends.
- Stored session transcripts are for the admin key only. `/runtime/explore`, `/runtime/library`
  and the compact `/runtime/context` view used to list, search and page verbatim transcript text for
  an agent read key under scope `all`; for a read key they now act as if no transcript exists (404,
  no cards, no search hits, no link counts). The admin key still reads them.
- A key or typed confirmation with a character outside ASCII is refused like any wrong one
  (401, or 400 for the memory-reset phrase) instead of a 500. The admin key from the
  environment, the conversation key, a listener's ingest key and the reset confirmation were
  compared as text, and `compare_digest` raises on non-ASCII text; they are compared as bytes
  now, as the correction key already was.

## 0.7.0

- With `MEMORYGATE_PI_URL` and `MEMORYGATE_PI_SERVICE_KEY` set, every model call (evidence
  analysis, Memory Lab answers) goes to Pi `POST /inference`, role `memory-sorting`, so the
  owner picks the model in Conker: Settings, Models, Memory sorting. MemoryGate's own model
  setting then reads "Chosen in Conker: Settings, Models, Memory sorting": `GET
  /system/ai-runtime` gains `managed_by`, `editable` and `chosen_in`, `PUT` answers 409, and
  `GET /runtime/health` gains `model_choice`. When Pi has no answer, MemoryGate falls back
  exactly as when its local model is unavailable. Without the two settings nothing changes;
  the stored setting is kept.
- Boundary tests through the whole HTTP API on throwaway stores (SQLite, Qdrant local mode):
  forget, supersede, scoped retrieval, a down vector index and admission. Fixes they found:
  two simultaneous resolutions of one contradiction now answer 409 instead of 500; an admin
  text edit (`PATCH /memory/{id}`) replaces the summary too, so reads and word search stop
  serving the old text; compact runtime context now says when retrieval fell back to word
  matching, as the full context already did.
- `PUT /runtime/corrections/remember/{request_id}` takes an optional `origin`: `owner`
  (the default, unchanged: `source_type` `user`, confidence `high`, audit
  `owner_stated_memory`) or `conker` for text Conker wrote itself, stored as `source_type`
  `assistant`, confidence `medium`, history note "Conker-written statement", audit
  `conker_stated_memory`. Compact context, library and explore cards now carry each
  memory's `source_type` too. A `/memory/write` refused as low value no longer keeps its
  text in the audit log (only its length, score and reason). `/memory/search` now also
  finds memories still queued for the vector index by their words (`retrieval_path`
  `lexical`, mode `hybrid` beside semantic hits) and reports `pending_vector_index`.
- Tests keep their files in pytest's `tmp_path` only. `services/api/tests/conftest.py` and
  `integrations/conftest.py` stop and fail any test that writes into the repository, and refuse a
  `--basetemp` or temporary directory inside it. The mutation drill
  (`scripts/memory_mutation_drill.py`) used to leave every run in `.test-runs/` in the checkout; it
  now uses the OS temp directory. `test_skills.py` no longer points `DATABASE_URL` at a SQLite file
  in the working directory.
- Through Pi, each AI proposal now records which `model` and `provider` answered (new nullable
  `analysis_objects` columns, added at startup; older rows stay empty), shown by `GET
  /evidence/analysis` and the Evidence and Pipeline screens. `POST /runtime/ask` answers carry
  `model` and `provider` too, and `GET /runtime/health` gains `model_choice.last_answer` when
  Pi chooses. The dashboard's Memory Lab and Runtime screens say the model is chosen in Conker
  (Settings, Models, Memory sorting) and which model answered last; they say "Ollama" only on
  the direct path. Stored names (`local_ai_proposal`, stage `local_ai`) are unchanged; the
  screens label them "AI proposal".
- The mutation drill runs in CI. Five of its mutants no longer matched the code they were meant to
  break; they now break the same rules where those rules live today (the bootstrap key binding,
  the vector outbox retry, the backup table list). The drill checks every anchor before it runs
  anything and stops, naming each mutant whose anchor moved, so none is ever skipped.

## 0.6.0

- The API container runs with a read-only root filesystem, every Linux capability dropped except
  DAC_OVERRIDE (needed to write backups into an owner-owned host folder) and no-new-privileges;
  `/data` is a volume, `/tmp` a small tmpfs. CI's contract job runs it that way.
- Owner-approved statements: `PUT /runtime/corrections/remember/{request_id}` stores one
  approved fact word for word under the correction capability, once per request, with a
  receipt. Pi uses it when the owner approves a memory Conker asked to save.

## 0.5.1

- The image no longer carries the base image's setuptools and wheel. The service never
  imports them, and their vendored copies had published advisories (wheel 0.45.1,
  jaraco.context 5.3.0) that the new image scan found. CI now checks that the built image
  still imports its app.

## 0.5.0

- Honor a message from the owner that starts "remember that ..." or "don't remember ..."
  (English or Russian) in their own attributable conversation; listener and other sources
  never carry one. A declined item is filtered without erasing its source evidence and cannot be
  promoted later by the background worker. Receipts gain one nullable field; earlier
  admission decisions are never reinterpreted.
- Literal recall searches and ranks the whole authorized corpus in SQL before applying its
  bounded limit, so older relevant memories are no longer crowded out by newer rows that
  merely share a common word. LIKE syntax in queries is matched literally.
- Correction receipts are bound to the exact payload the owner approved.
- Update anyio to 4.14.2.
- Update certifi to 2026.10.19 for a published advisory, and the dashboard's build
  tooling and React Router for published advisories.
- A weekly security check (`.github/workflows/security.yml`) audits every dependency this
  repository ships against the public advisory databases each Monday and whenever they
  change, and fails loudly.
- Outbound HTTP clients now ignore ambient proxy environment variables and
  refuse redirects, with live hostile-proxy and static policy regression tests.
- Processing jobs now use durable, renewable leases. Restarted workers reclaim
  abandoned and expired jobs, concurrent workers cannot own the same claim, and
  database fencing prevents a stale worker from committing after lease takeover.
- Memory, observation, entity, and skill vector changes now use a transactional
  PostgreSQL outbox. Stable-ID Qdrant operations replay after outages or crashes,
  deletes supersede unapplied upserts, and pending failures remain observable.

## 0.4.1

- Keep embedding-provider probes atomic and return stable snapshots so concurrent
  health, retrieval and failed embedding requests cannot expose partial or cleared
  cache entries. Invalid provider responses report unavailability instead of HTTP 500.
- Add regression coverage for concurrent probes, cache invalidation and malformed
  provider identities; preserve lexical fallback and existing authorization scopes.

## 0.4.0

- Publish the scoped, paginated memory library and progressive typed exploration.
- Include revision-safe corrections, source-bound complete forgetting, deletion
  tombstones and isolated recovery verification.
- Preserve bootstrap revocations and namespace-bound memory authority.

## 0.3.0

Bilingual admission, honest health and cost, and durable deletion.

- **Russian is admitted at parity with English.** The signal filter scored
  ordinary Russian preferences at zero and dropped them before storage; it now
  scores them like their English equivalents, so half the owner's life is no
  longer silently unrecordable.
- **Cross-service deletion.** A forget request from Pi removes the evidence in
  the namespace that stored it and returns a receipt bound to that namespace.
- **Truthful health.** A failed collection inspection reports `degraded` with
  the reason instead of returning `ok`.
- **Bounded, honest cost.** Unmetered hosted generation refuses rather than
  silently spending, recording an estimate or an explicit unknown cost. Oversized
  ingestion returns `413 CONTENT_TOO_LARGE` with `retryable=false` and the limit,
  so a worker does not retry a payload that can never fit.
- **Revocation survives bootstrap** — a read key the owner revoked or narrowed
  is not reactivated on restart.
- `cryptography` pinned to `50.0.0`, covering CVE-2026-34180 and the PKCS#7
  advisory.


- Bootstrap read-key configuration now only seeds missing authority. Existing
  revocations, agent assignments, labels and credential changes survive restart;
  renaming a revoked key cannot create an active copy of the same credential.

- Add scoped, idempotent Pi conversation ingestion with atomic source/analysis/memory
  lineage, durable deletion tombstones and retryable vector work.
- Admit ordinary Russian preferences at the same threshold as English, including
  short statements; preserve source attribution, dates and uncertainty in context.
- Follow direct conversation lineage on source invalidation; unsupported memories
  leave active retrieval. Flush changed support links before checking remaining support.
- Include conversation receipts in logical backups and expose index lag in retrieval.



## 0.2.0

Two defects made MemoryGate untrustworthy in opposite directions: it authenticated
nobody, and it answered retrieval questions with noise. Both are fixed by making the
service refuse to run in a state it cannot honestly serve, and by reporting degradation
instead of hiding it.

### Contract changes

- `GET /health` now runs real probes against PostgreSQL, Qdrant and the embedding
  provider. It returns `{status, service, degraded[], dependencies{}, age_seconds}`
  instead of a hardcoded `{"status": "ok"}`. Probe detail is coarse because the route is
  unauthenticated; results are cached for five seconds and carry their age.
- `POST /runtime/context` and `POST /memory/search` gained a `retrieval` block
  (`mode`, `semantic.{status,component,reason}`), and every returned memory gained
  `retrieval_path` (`semantic` or `lexical`). `/runtime/context` also states the
  degradation inside `usage.instruction`, which is the text a reading model sees.
- Write responses (`/memory/write`, `/memory/{id}` PATCH, `/entity/create`,
  `/entity/update`, `/observation/create`) gained an optional `indexing` object, present
  only when the row was committed but could not be added to the vector index.
  `/memory/write` also gained `novelty_check` when the near-duplicate check could not run,
  and entity/observation creation gained `dedup` for the same reason. Processing job
  results carry `indexing` on the same terms.
- `GET /auth/check` no longer returns the tier `disabled`; there is no unauthenticated
  tier left to report.

### Security

- **The service refuses to start with no admin key configured.** `verify_admin_key()`
  used to return `True` when neither `MEMORYGATE_ADMIN_KEY` nor a database-managed key
  existed, and the stock compose file set neither - so a freshly composed instance served
  every route, including `POST /system/memory-reset`, to anything that could reach the
  port. Startup now fails with an error naming the exact fix, and the check itself fails
  closed regardless. An environment key must be at least 16 characters.
- **CORS no longer defaults to `*`.** The default is the bundled dashboard's own origins.
  `MEMORYGATE_CORS_ORIGINS=*` remains available as a documented development override and
  logs a warning at startup.
- `POST /system/memory-reset` keeps its `RESET MEMORY` phrase, now compared in constant
  time through one shared helper and covered by a test proving a valid admin key alone is
  not sufficient.

### Retrieval

- **`EMBED_MODEL=hash` is removed.** It derived every vector component from
  `sha256(index:text)`, so near-identical sentences produced uncorrelated vectors and
  cosine similarity over them was noise. `embed_text()` now raises `EmbeddingUnavailable`
  rather than returning a plausible-looking vector, and callers fall back to lexical
  retrieval while saying so.
- The bare `except Exception` around vector search in `_build_context` is gone. The
  retrieval path is chosen from an explicit status check, so a degraded answer is labelled
  as one instead of being indistinguishable from a healthy one.

### Reliability

- A vector-index or embedding failure after a successful Postgres commit no longer
  returns 500 with the row already written. `index_after_commit()` reports the failure
  and the caller surfaces it.
- The API starts when Qdrant is unreachable instead of crash-looping, and re-attempts
  collection creation on first use so it heals once the index returns.
- `services/api/app/services/qdrant_stub.py`, which nothing imported, is deleted.

### Repository

- Added `.env.example` and `services/api/requirements-dev.txt`; the README documents
  creating the external `conker_net` network and running the test suite.
- Fixed a test that leaked a SQLite handle and failed only on Windows.

- F10: Refuse direct hosted generation without a shared-budget adapter; audit its estimated or unknown cost and expose blocked_budget in runtime status.

- F7: Report unverified Qdrant collections as degraded with the collection and failure class.

- F8: Return a content-free, permanent HTTP 413 error for oversized conversation ingestion; exactly 16,000 characters still works.

- F12: Pin cryptography 50.0.0 to cover both the bundled OpenSSL CVE-2026-34180 fix (48.0.1) and the PKCS#7 oracle fix (50.0.0), preserving Fernet compatibility.
