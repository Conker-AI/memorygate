# Pi conversation evidence

`PUT /runtime/conversation/{message_id}` accepts `session_id`, `content` and
`created_at`. The dedicated `X-MemoryGate-Conversation-Key` capability is bound to
`MEMORYGATE_CONVERSATION_AGENT_ID` on the server. It cannot choose an agent in the
payload, use other sources, administer memories, or invoke a planner. Admin/read
headers do not authorize this endpoint. Pi needs a separate agent read key for
`POST /runtime/context`.

The receiver derives evidence, analysis and memory IDs from the agent and Pi
message ID. Receipt, evidence, admission analysis, quoted memory and lineage
commit atomically. PostgreSQL locks the receipt row; SQLite uses its writer lock.
The same ID/content returns the existing receipt; conflicting content returns
409. No model output or generated interpretation is promoted through this path.
Quoted statements have medium confidence and `do_not_generalize=true`; their
source dates and citations reach context retrieval.

The new path deliberately does not call the existing listener's LLM pipeline.
That pipeline commits processing stages separately and is unsuitable for an
idempotent conversation receiver. Existing listeners continue to use it.

## Russian admission

The threshold remains **0.3**. The filter normalizes Unicode/case and `ё`/`е`,
recognizes Russian preference, routine, relationship and goal expressions,
including common inflections, and treats Russian acknowledgments as low signal.
Both “I prefer to train before school” and “Я предпочитаю тренироваться перед
школой” score 0.3 and become attributable memories. The existing listener also
loses its four-word gate: “Люблю плавать” is useful despite having two words.
English keyword matching now respects word boundaries, so `against` cannot
match `again`. This remains a bilingual heuristic, not a claim of complete
language understanding or reliable inference about the owner.

## Deletions and indexing

`DELETE /runtime/conversation/{message_id}` requires the same source capability.
It commits a durable tombstone even if ingestion never arrived. Subsequent PUTs
return `deleted`, with an honest forgotten citation, and cannot resurrect content.
Source invalidation also marks unsupported conversation memories for review.
Owned analysis, memory, revisions and conflicts are removed by forgetting; source evidence and
referencing audit payloads are redacted. Independent derivatives and backups
require separate retention/deletion handling.

The conversation receipt and the shared `vector_outbox` intent commit in the same
SQL transaction. The worker locks one desired-state row while applying its stable-ID
Qdrant operation, so a later delete cannot be overtaken by an older upsert. Failed
writes remain pending with a coarse error and bounded backoff. Retrieval reports
both the compatibility `pending_conversation_index` count and the shared pending/
failed vector counts. A stale vector never supplies content without an active SQL
memory row. Logical backups include receipts and vector intents; the umbrella's
full PostgreSQL dump includes both tables automatically.

## Deployment and proof

Set `MEMORYGATE_CONVERSATION_KEY` and `MEMORYGATE_CONVERSATION_AGENT_ID`; see
Pi's `docs/memory.md` for matching variables. The new table is created at startup.
Keep the existing embedding/index services configured for semantic retrieval.
The existing bootstrap read-key resurrection defect is not repaired here.

Run the normal tests and the nine-mutation drill:

```sh
PYTHONPATH=services/api python -m pytest services/api/tests
PYTHONPATH=services/api python scripts/memory_mutation_drill.py
```

With both checkouts present, the cross-service drill uses actual API handlers,
authentication, separate SQLite stores, retries, restart, retrieval and forgetting.
Only socket transport and the reply-generating language model are replaced:

```sh
PYTHONPATH=services/api:../pi python -m pytest integrations/test_pi_memory.py
```

Its default mode proves **literal fallback** in both languages and asserts that
degradation reaches Pi. It does not claim to prove multilingual semantic recall.
To require the real configured Embeddings/Qdrant services instead (export matching
`EMBEDDINGS_KEY`, `EMBEDDINGS_URL` and `EMBED_DIMENSION` first):

```sh
MEMORY_E2E_SEMANTIC=1 PYTHONPATH=services/api:../pi \
  python -m pytest integrations/test_pi_memory.py
```

That mode fails if real indexing or semantic retrieval fails; it never skips or
substitutes synthetic vectors. Use disposable test index collections through
`QDRANT_COLLECTION`. For a host where Qdrant is not exposed, add `MEMORY_E2E_LOCAL_INDEX=1` to use
Qdrant's real embedded index in a temporary directory. This still requires actual
vectors from the configured embedding service. Semantic mode runs English,
Russian, and both cross-language directions, ranking the relevant preference
above an unrelated reading preference before checking deletion.

The patch was verified with the running `qwen3-embedding:0.6b` service and the
embedded Qdrant index, plus the lexical fallback drill. PostgreSQL concurrency and
the server/container form of Qdrant still require a deployment drill; Docker
administration was inaccessible.
