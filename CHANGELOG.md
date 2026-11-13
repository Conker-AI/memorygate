# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry - replacing a module has to be
a decision with visible consequences.

## Unreleased

- **Two patterns that start alike stay two patterns.** A pattern was named by the first 60
  characters of its hypothesis, so two clusters sharing that beginning wrote into one pattern,
  each run replacing the other's observations and counting a new confirmation. A long hypothesis
  now adds a short hash of the whole text to the name; a pattern under the old name stays with
  the cluster whose observations it holds. Contradicted or archived observations no longer build
  a pattern.

- **A lockout stops wrong keys, never the right one.** After five wrong keys from one address,
  every key from that address was refused for 300 s, so one misconfigured caller behind a proxy
  or the host network locked out the rest, and failures never expired. Now the key is checked
  first: a right key always passes (and clears the count), a wrong one is counted and refused
  while the address is locked, and failures older than 15 minutes no longer count. Applies to
  the admin key, read keys, listener keys and the current-key confirmations.

- **A memory that becomes a phase gets its two-week review date.** Editing a memory to a phase,
  a duplicate write that upgrades one to a phase and a listener promotion classified as a phase
  left the review date empty, so the startup catch-up later dated it from when the memory was
  made, already in the past for an old one. Each now sets it to two weeks from the change.

- **Editing an evidence source keeps its listener working.** New secrets on `POST /evidence/sources`
  replaced the whole secret map, dropping the ingest key from `rotate-ingest-key`, so the listener
  was cut off; the ingest key now stays. `POST /evidence` refuses a disabled source (409), and a
  source's "last ingested" is the arrival time, not the event's own time.

- **Memory upkeep candidates (read-only).** `GET /runtime/memory-upkeep` (read key or admin key,
  the header's agent only) lists exact and near duplicates, open contradictions, memories past
  `valid_until` or their review date, and active memories not recalled for `unused_days`
  (default 90), each with the ids and a reason; `limit` caps each group, `total` counts all.
  It changes nothing. Recall (`/runtime/context`, `/runtime/ask`) now stamps a new
  `last_recalled_at` column on the memories it returns, at most once an hour, best effort and
  without bumping the revision; memories recalled before this release read "never recalled".

- **A deleted lineage link stays deleted.** Every start rebuilt the lineage links held in the
  older id lists, so a link the owner deleted came back at the next start. The rebuild now runs
  once per install (a marker in `data_migration_steps`); the id lists stay what forgetting reads.

- **Memory search without the vector index matches words.** When the index was down or found
  nothing, `POST /memory/search` matched the whole query as one piece of text, so "release
  pipeline" found nothing in "the pipeline for the release". The fallback now uses recall's word
  search (active memories, best match first).

- **No model answer, no AI analysis.** When no model answered (Pi down, local model off, hosted
  call refused), evidence processing stored an empty `local_ai_proposal` analysis, so the
  evidence counted as analysed and was never analysed again. Now nothing is stored.

- **A right key is stretched once, not on every request.** Each read request with a dashboard
  admin key set ran PBKDF2 for the admin miss and again for every read key before the match, so
  Pi paid that on every turn. A key that verified is now remembered for the process (by a
  peppered tag, beside the hash it matched, at most 256), and a read key is checked against read
  keys first. A changed admin key or a revoked read key is refused at once, and a wrong key still
  pays the full check.

- **The audit list is bounded.** `GET /audit` returned every row ever written; it now returns the
  newest `limit` rows (default 500, at most 2000). `/audit/metrics` counts with GROUP BY instead of
  loading every row of the window.

- **A reset leaves no trace of what it removed.** Correction receipts naming a removed memory are
  removed with it (a repeated correction request would otherwise answer for a memory that no
  longer exists), and a dated reset takes the removed entities' ids out of the observations and
  patterns it keeps. Forget receipts stay, so a forgotten memory never returns.

- **The briefing shows only live evidence.** A watch memory that lost a conflict or was
  marked unsupported stayed in the pre-session briefing for 14 days, and a contradicted or
  archived emotional observation could still set its mood. Watch flags now come only from
  active memories, and the mood ignores contradicted and archived observations.

- **The conflict check reads whole words and keeps the owner's words in recall.** It counted
  "not" inside "knot" or "cannot " and matched long words as substrings over the first eight
  only. Now it compares whole words ("not", "never", "no longer", "stopped", "cannot", "-n't"
  and at least two shared words over four letters). A memory in the owner's own words (source
  `user` or `owner_statement`) is no longer taken out of recall because of one that is not: the
  conflict is still recorded, and only the other memory goes to review.

- **Health checks the model in use.** The local model's health reported the env default
  (`OLLAMA_MODEL`), while calls send the model stored in the runtime setting, so after the owner
  changed the model the status could say ready while every call failed. Health now names and
  checks the stored model.

- **A recovery replay removes every copy of a forgotten memory's text.** Replaying a memory's
  deletion receipt into a restored backup deleted only the memory row, so its revision snapshots,
  the conflicts naming it and the text in its audit entries came back with the backup. The replay
  now removes and redacts them as a live forget does, also when the row is already gone, and
  turns the memory's (or entity's or observation's) vector outbox row into a delete, so neither
  its stored text nor a pending re-index survives the restore.

- **A write is not lost to a memory under review.** `POST /memory/write` took a memory with the
  same text as its duplicate whatever its status, so a write equal to a memory under review or
  expired answered ok with `duplicate_of` and nothing became active; it also looked only at the
  newest 100. Now only an active memory of the agent counts, however old, and two writes of one
  text are serialised (Postgres).

- **Memory and entity lists page.** `GET /memory` and `GET /entity` returned the newest 100 rows
  and nothing more, so older ones could not be listed, reviewed or deleted through them. Both take
  `limit` (1-500, default 100) and `offset`.

- **The owner's correction stays in recall.** When the owner remembered a fact that contradicts
  an older one in his own words, both went to review and recall lost the old and the new fact
  under a "remembered" receipt. Now the newest owner statement stays active and only the older
  one goes to review; Conker-written statements keep the old rule. The remember receipt also
  carries `memory_status`, so a caller can tell a memory that waits for review.

- **"Don't remember" keeps no words.** A conversation message the owner asked not to keep
  ("don't remember ...", "не запоминай ...") used to be stored word for word as evidence (only
  the memory was skipped). Its evidence now holds no text (`normalized_payload` is
  `{"withheld": "owner_declined"}`, empty summary); the receipt and the analysis still record
  that it was declined. Messages filtered for low value keep their words as before.

- **A skill is delivered whole or refused, and one big skill no longer hides the rest.** A skill
  body was accepted up to 12000 characters but cut to 8192 bytes on every read, so an edit saved
  the cut text. Now a skill whose title and body exceed 8192 bytes is refused (422) on create and
  edit, and reads return the stored text. In `/context/skills` a skill that does not fit what is
  left is skipped instead of ending the list, so smaller skills after it are still delivered.

- **Observation and pattern search no longer load every row.** Search (and pattern promotion)
  read every observation or pattern of the agent and filtered in Python. Status, text and entity
  filters now narrow in SQL, the exact checks stay, and search stops at its 50 results.

- **Creating an observation no longer reads every unconfirmed one.** The observation budget
  loaded all unconfirmed observations of the agent on each create; it now counts them in SQL and
  loads only the ones it archives (most exposed first, oldest first among equals).

- **Deleting an entity takes what refers to it.** `DELETE /entity/{id}` used to remove only the
  entity row; its history snapshots (old names, notes and attributes), events, edges and lineage
  links stayed, and observations and patterns kept its id. Now they go with it (each link leaves
  a `link` receipt), and recovery replay of an `entity` receipt removes the same set from a
  restored backup.

- **An observation cap of zero no longer breaks observation create.** `PUT /config/{agent}`
  accepted any `max_observations`, so 0 or less made every create answer 500 (`max()` of an
  empty list). The config now takes 1..100000 and thresholds 0..1 (422 otherwise), and a cap
  below one already stored counts as one. Lowering the cap archives enough of the most-exposed
  unconfirmed observations in one call (it used to archive one per create), inside the create's
  own transaction.

- **Remembering no longer reads every memory.** The duplicate check and the conflict check each
  loaded every active memory of the agent. Both now narrow in SQL (rows containing the statement,
  rows sharing a long word) and keep the exact whole-word checks.

- **The 600-line check covers the dashboard.** `scripts/check_file_sizes.py` skipped `.jsx`
  (the dashboard's own files) and missed a last line with no newline; the one file over the
  limit, the Entities screen (662), is split (its three dialogs moved to
  `components/EntityModals.jsx`). No behaviour change.

- **Id lists name only rows that exist.** Observation and pattern create and update stored any
  ids in `entity_ids`, `related_observation_ids`, `observation_ids` and `applies_to_entity_ids`,
  so a deleted or another agent's id could be written back. Such a request is now refused (422
  `unknown_ids`, naming the ids) and stores nothing.

- **The classifier matches whole words.** A memory type came from substring hits, so "again"
  in "against" made a memory `watch`, "sad" in "Sadie" or "crusade" made it `phase`, and "train"
  in "constraint" counted as a domain word. Phrases now match as whole words (a curly apostrophe
  counts as a straight one). One feeling word also fired two phase signals, because the two
  emotion lists overlap; a phase now has two signals, a feeling and a timeframe, so "sad this
  week" is `medium`, not `high`. The unused `NEGATION_WORDS` list is gone.

- **A deleted observation takes the patterns built on it.** A pattern that still named other
  observations kept its name, description and interpretation, which may carry what the deleted
  observation said. Now every pattern that names it goes, each with a deletion receipt (so a
  restored backup forgets them too); promotion builds a pattern again from the observations left
  when they still qualify.

- **Merging entities keeps lineage and the graph whole.** The startup dedup and the manual
  merge (`POST /entity/merge`) now share one routine: lineage links to the removed entity move to
  the kept one, an edge between the two (a would-be self-loop) or one the kept entity already has
  goes, the removed entity's vector is queued for deletion (the startup dedup left it in Qdrant),
  and a malformed tags or attributes value no longer stops startup.

- **Patterns can be deleted, and a deleted observation leaves them.** There was no way to delete
  a pattern, and deleting an observation left its id, its lineage links and every pattern built
  from it. `DELETE /pattern/{id}` now deletes a pattern of the calling agent (404 for another
  agent's) with its lineage links and a `pattern` deletion receipt. Deleting an observation
  removes its lineage links, its id in other observations and in patterns, and a pattern left
  with no observation (with its receipt). Recovery replay of `observation` and `pattern`
  receipts removes the same set from a restored backup. A pattern that still has other
  observations keeps its text.

- **Lineage stays inside one agent.** `POST /lineage/links`, `GET /lineage/{type}/{id}`,
  `DELETE /lineage/links/{id}` and an episode's `evidence_ids` now answer 404 for an object
  of another agent (the `X-Agent-Id` header), as for a missing one. Before, they read and
  linked any agent's objects by id.

- **No more transcript archive writes** (contract change). Transcripts live only in Pi
  (ADR-0002), and nothing in Conker wrote here, so `POST /transcripts` and the SoulGate
  flags (`POST /transcripts/{id}/reprocess`, `/mark-processed`, `processed_by_soulgate` in
  replies) are gone. Rows already stored can still be listed and read, and now deleted:
  `DELETE /transcripts/{id}` (admin key, the agent's own row) leaves a content-free
  `transcript` deletion receipt, so recovery replay keeps it deleted after a restore. The
  dashboard's Sessions screen has Delete instead of "Re-process with SoulGate".

- **Each tag gets a GitHub Release page and an SBOM.** The Release page's notes are the
  version's CHANGELOG section. Every image carries an SPDX SBOM (BuildKit attestation) inside
  the image index, and the signed build provenance covers that index digest. Check one with
  `gh attestation verify oci://IMAGE@DIGEST --owner Conker-AI`.

- **Backups keep correction and forget receipts and the audit trail** (`memory_corrections`,
  `memory_forgets`, `memory_audit`), so a retried correction or forget answers with its receipt
  after a restore. A test fails on any table that is neither backed up nor listed as left out.
- **A listener can erase an event it sent** (`DELETE /runtime/listeners/{source_key}/evidence/{id}`,
  same key): the evidence, its analyses, links, job results and the listener memories it alone
  supported go; recovery replays the erase (`listener_evidence` receipts). New endpoint, no change
  to existing ones. Pi uses it when the owner deletes an imported chat.
- **Forgetting a memory takes the evidence only it used.** Evidence no other memory relies on is
  tombstoned with it; evidence another memory still uses stays. (#54)
- **A memory forget blanks only its own agent's episodes**, never another agent's episode that
  shared the evidence; a regression test runs every forget path. (#55)
- **Repository tooling, no behaviour change.** The shared .editorconfig, pre-commit hooks,
  CODEOWNERS and Renovate config; a pyright type check (model datetimes typed as Python datetimes,
  62 -> 5 errors, the baseline may only shrink); refusal tests for the pattern, observation,
  entity and lineage routes, so CI fails below 84% coverage (was 76%); leak checks match whole
  words, not letters inside random key hashes. (#53, #57, #58, #74)

## 0.8.2

- **The image takes Debian's zlib security fix** (zlib1g CVE-2026-76844 and CVE-2026-85091,
  fixed in 1:1.3.dfsg+really1.3.1-1+deb13u1). Rebuild only, no code change.

## 0.8.1

- **The image takes Debian's security fixes newer than its base** (apt, CVE-2026-109518).
  No code change.

## 0.8.0

- **Refused request bodies are never echoed back.** A 422 lists only `loc`, `msg` and `type` for
  each error. FastAPI's default repeated the input, so a malformed `POST /auth/change-key` or
  `PUT /system/ai-runtime` sent the current admin key, or a provider API key, back in the error.
- **Writes that cannot half-happen.** Ingest commits the evidence and its processing job
  together, so evidence can no longer sit `pending` with no job; a backup is written to a
  temporary file and moved into place, so a partial file is never listed; a retried job reuses
  the analysis rows it already wrote; two concurrent writes of the same memory no longer both get
  in (a Postgres advisory lock on agent and text); an episode's end time only moves forward.
- **Dashboard and MCP.** A path with an encoded `../` cannot serve files outside the dashboard
  build; unknown MCP methods answer "Method not found" and batch requests get an error; the
  context tool's `query` is bounded to 2,000 characters; deleting a vector from a collection that
  was never created returns early.

- Retrying a job that a worker is still running (its lease has not expired) is refused with a 409.
  It used to be queued again and cleared the lease, so a second worker could process the same
  evidence while the first was still on it.
- A job whose stored evidence payload cannot be read - not JSON, or JSON that is not an object -
  used to raise inside the worker loop, leaving the job `processing` with its lease; when the
  lease ran out the next poll claimed it again and failed the same way, forever, one wasted claim
  per lease. The payload is now parsed in `run_job`, which fails the job once ("evidence payload
  is not readable") and quarantines the evidence, the same outcome `process_evidence` already
  gives a job whose processing raises.
- A malformed time in a request field (`occurred_at`, `session_start`/`session_end`,
  `occurred_start`/`occurred_end`, `review_by`) answers 422 naming the bad field instead of a
  500. Each route module had its own parse helper that let the `ValueError` from
  `fromisoformat` escape; they share `app.core.timestamps.parse_timestamp` now.
- A memory reset also removes the conversation receipts of the evidence and memories it removes.
  Before, a message Pi delivered again after a reset was answered as admitted with a memory that no
  longer existed, so it could never be learned again. Forgetting tombstones survive the reset: a
  forgotten message still never comes back.
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
