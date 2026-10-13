# Vector outbox

PostgreSQL owns memory state. Qdrant is a rebuildable derived index, but unfinished
index work must still be durable. Every memory, observation, entity, or skill write
records one latest desired operation in `vector_outbox` before its SQL transaction
commits. Deletes use the same row and supersede unapplied upserts.

The worker selects a due pending row with `FOR UPDATE SKIP LOCKED` and holds that
row lock while calling the existing Qdrant abstraction. Operations use stable object
IDs, so replay after an uncertain result is idempotent. Success marks the generation
complete in the same database transaction that held the lock. Failure keeps it
pending, records a coarse non-secret error, and applies bounded exponential backoff.

The startup migration is additive and idempotent. It seeds missing upsert intents
from existing source rows and delete intents from durable deletion receipts without
overwriting a newer outbox generation. Runtime context exposes `pending_vector_index`
and `failed_vector_index`; a nonzero value means semantic retrieval may be incomplete.

This is an at-least-once bridge, not a distributed transaction. PostgreSQL and
Qdrant cannot commit atomically. Correctness comes from durable desired state,
per-object serialization, stable-ID idempotence, and replay. An indefinitely hung
Qdrant request can hold one object's outbox row and a database connection until the
client timeout releases it.
