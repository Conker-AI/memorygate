"""The reset names every table it removes or keeps, and its statement count does not grow per row.

A table added later and forgotten in memory_reset_service fails the first test, so a reset cannot
silently skip it (or silently take it). The second proves the batching: forty-five times the rows
cost a handful more statements (one per chunk of ids), not one or more per row.
"""
from datetime import datetime

import app.main  # noqa: F401 - registers every model on Base.metadata
import pytest
from app.core.db import Base
from app.models.entity import Entity
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.services import backup_service
from app.services.memory_reset_service import RESET_TABLES, REWRITTEN_TABLES, reset_memory
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# Tables a reset leaves alone on purpose: access and recovery state, sources, and tombstones.
KEPT_TABLES = {
    "agent_access_keys": "credentials",
    "agent_configs": "settings",
    "ai_runtime_settings": "settings",
    "auth_settings": "credentials",
    "bootstrap_authorities": "credentials",
    "evidence_sources": "source configuration",
    "deletion_receipts": "forgetting tombstones: ids only",
    "memory_forgets": "forgetting tombstones: ids only",
    "recovery_holds": "recovery state",
    "recovery_verifications": "recovery state",
}


def test_every_table_is_removed_kept_or_rewritten_on_purpose():
    tables = set(Base.metadata.tables)
    kept = set(KEPT_TABLES)
    assert tables - RESET_TABLES - kept - REWRITTEN_TABLES == set()
    assert RESET_TABLES & kept == set()
    assert REWRITTEN_TABLES & (RESET_TABLES | kept) == set()
    assert tables >= RESET_TABLES | kept | REWRITTEN_TABLES  # no stale names


@pytest.mark.parametrize("reset_from", [None, datetime(2026, 1, 1)])
def test_the_statement_count_does_not_grow_with_the_rows(tmp_path, monkeypatch, reset_from):
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path / "backups"))
    counts = {}
    for size in (10, 450):
        engine = create_engine(f"sqlite:///{tmp_path / f'reset-{size}.db'}")
        Base.metadata.create_all(engine)
        with sessionmaker(engine, expire_on_commit=False)() as db:
            for n in range(size):
                db.add_all([
                    Memory(id=f"m-{n}", agent_id="owner", text=f"memory {n}", created_at=datetime(2026, 5, 1)),
                    Entity(id=f"e-{n}", agent_id="owner", entity_type="person", name=f"Sam {n}",
                           created_at=datetime(2026, 5, 1)),
                    ObjectLink(source_type="memory", source_id=f"m-{n}", target_type="entity",
                               target_id=f"e-{n}", relationship="about"),
                ])
            db.commit()
            statements = []

            def count(*args, bucket=statements):
                bucket.append(args[2])

            event.listen(engine, "before_cursor_execute", count)
            removed = reset_memory(db, reset_from)["removed"]
            event.remove(engine, "before_cursor_execute", count)
            assert removed["memories"] == size and removed["entities"] == size
        counts[size] = len(statements)
        engine.dispose()
    # Chunks of 100-500 ids add a few statements; the old form added several per row (thousands).
    assert counts[450] - counts[10] <= 30, counts
