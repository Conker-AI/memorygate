# Progressive memory exploration

MemoryGate preserves its stored graph and exposes bounded views rather than
injecting every source into every answer. No new vector database or inferred
relationship generator is introduced.

## API and MCP

`POST /runtime/context` accepts `compact: true`. Existing callers retain the old
response shape unless they opt in. Existing lexical/semantic retrieval selects
candidates; compact output contains object identities, short previews, status and
confidence where stored, available fields, and counts by connection type. No raw
transcript, complete property dictionary or general briefing is injected.

`POST /runtime/explore` requires the read key and header namespace. Supply:

```json
{"object_type":"memory","object_id":"returned-id","operation":"connections","limit":12}
```

The response includes typed directed links, compact neighbor cards and `next_after`.
Use `after` for the next page or `relationship` to narrow exploration. Both endpoints
must be visible: cross-namespace, invalidated evidence, inactive memory and missing
nodes are excluded from counts as well as content. Entity relationships and stored
ObjectLink provenance are included without merging their semantics. Entity strength
is not relabeled as confidence. These are stored relationships, not every possible
similarity or an inference that shared sources imply shared meaning.

To read a source or object field:

```json
{"object_type":"evidence","object_id":"returned-id","operation":"content",
 "field":"normalized_payload_json","offset":0,"characters":4000}
```

Choose a field from `available_fields`; follow `next_offset` for long content.
Source raw payloads and connector secret/configuration fields are never exposed.
The page maximum is 25 links or 16,000 content characters. Pages are live views:
edits/deletion may change subsequent pages, and access is rechecked each time.

The stdio MCP server now exposes `memorygate_context` (compact by default) and
`memorygate_explore`. Both use the same operator-configured `MEMORYGATE_AGENT_ID`
and `MEMORYGATE_SCOPE` (`all`, `none`, `selected`, `conversation`). Selected scope
requires `MEMORYGATE_MEMORY_IDS` as a JSON array; conversation scope requires
`MEMORYGATE_SESSION_ID`. The model cannot override these environment settings.
Restricted scopes currently expose only eligible memories, not their broader
source graph. Use a read key for the intended namespace. As before, `all` is the
default when the operator does not configure a narrower scope.

This is available to MCP consumers now; Conker's Pi context transport remains on
its established response contract until separately wired. No dashboard transport,
uploaded binary store, custom entity type registry or new UI was added here.

## Model decision

Graph lookup, pagination and access checks do not need another model. Existing
retrieval provides candidate selection; the answering agent chooses what to expand.
Do not require a paid call or GPU to traverse saved edges.

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) is designed
for typed decisions rather than generated summaries. A possible later role is
reranking small authorized candidate sets or choosing whether to expand. It must
not grant permissions, delete evidence or turn an inferred claim into a fact.

[Laya](https://github.com/NandhaKishorM/laya) is a plausible local candidate, not a
verified identification of the model the owner saw. Its repository describes
322M/421M checkpoints with 512/1024 context limits and reports 193–464 ms CPU
latency with preload; T4 results are faster. These are author measurements, not
our hardware benchmark. Short context and confidently wrong classifications are
material limits. No weights were installed, model hardcoded or paid inference run.

Before adding either, compare baseline retrieval with the proposed reranker on
real labeled questions: evidence recall, unsupported claims, token usage, latency
and wrong-scope results. Preserve a model-free fallback and choose the provider
through existing configuration. No universal optimality claim is warranted yet.
