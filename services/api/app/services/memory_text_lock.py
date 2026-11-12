"""One lock per memory text, shared by every path that reuses or erases a memory by its words.

The listener pipeline reuses a stored memory whose text matches an event (case-insensitive) and
links the event's analysis to it. Forgetting reads which evidence backs a memory and tombstones
it. On PostgreSQL both take this transaction-scoped advisory lock, so a link made by a worker is
either committed before the forget reads the backing set (and its evidence is forgotten too) or
made after the memory is gone (and the worker promotes a memory of its own). SQLite serialises
writers already (BEGIN IMMEDIATE), so it needs none. The lock is re-entrant within a transaction.
"""

from sqlalchemy import text

_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:agent_id), hashtext(:memory_text))")


def lock_memory_text(db, agent_id: str, memory_text: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(_LOCK, {"agent_id": agent_id, "memory_text": memory_text.strip().lower()})
