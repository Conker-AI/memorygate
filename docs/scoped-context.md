# Explicit runtime memory scopes

POST `/runtime/context` accepts `scope` (default `all` preserves existing behavior):

- `none`: no records or helpers.
- `selected`: requires distinct `memory_ids` (1–1000), returns only active records in the authenticated agent namespace and that selection.
- `conversation`: requires `session_id`, returns active memory with admitted conversation receipts in that session and namespace; citations stay within that session.

Restricted scopes do not call embedding/model helpers and do not include global briefing, entities, episodes or independent evidence. `include_evidence` does not override the scope. The existing `max_items` (1–30) bounds results; missing, foreign, inactive and deleted records are never substituted by broader matches. This is explicit selection, not relevance scoring. Unknown request fields and incompatible selectors are rejected to prevent misspelled filters from broadening retrieval.

This constrains data selection, not access authorization: existing runtime read credentials and agent namespace rules still apply. Pi must enforce its selected agent/session policy before choosing these fields. No frontend connection or deployment is included.

Temporary database verification: 31 scoped-context and conversation-memory tests passed, covering namespace/selection isolation, no broad helper calls, incompatible selectors and tombstone regressions.
