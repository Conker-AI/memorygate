"""Every table is in a backup or left out on purpose, and receipts survive a backup.

A table added later and forgotten here fails the first test, so the backup cannot silently
miss it again (R-E5: correction and forget receipts and the audit trail were missing).
"""

import json

import app.main  # noqa: F401 - registers every model on Base.metadata
from app.core.db import Base
from app.models.audit import MemoryAudit
from app.models.memory_correction import MemoryCorrection
from app.models.memory_forget import MemoryForget
from app.services import backup_service
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_every_table_is_backed_up_or_left_out_on_purpose():
    backed_up = {model.__tablename__ for model in backup_service._MODELS}
    tables = set(Base.metadata.tables)

    assert tables - backed_up - backup_service.EXCLUDED == set()
    assert backed_up & backup_service.EXCLUDED == set()
    assert tables >= backup_service.EXCLUDED  # no stale names


def test_receipts_and_the_audit_trail_are_in_the_backup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'backup.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path / "backups"))
    with sessionmaker(engine)() as db:
        db.add(MemoryForget(agent_id="owner", request_id="forget-1", memory_id="m-1",
                            payload_hash="a" * 64, revision=2, index_removal="removed"))
        db.add(MemoryCorrection(agent_id="owner", request_id="fix-1", memory_id="m-2",
                                payload_hash="b" * 64, previous_revision=1, revision=2))
        db.add(MemoryAudit(action="owner_forget", memory_id="m-1",
                           payload_json=json.dumps({"request_id": "forget-1"})))
        db.commit()
        result = backup_service.create_backup(db)
    engine.dispose()

    tables = json.loads((tmp_path / "backups" / result["filename"]).read_text())["tables"]
    assert [row["request_id"] for row in tables["memory_forgets"]] == ["forget-1"]
    assert [row["request_id"] for row in tables["memory_corrections"]] == ["fix-1"]
    assert [row["action"] for row in tables["memory_audit"]] == ["owner_forget"]
    assert not set(tables) & backup_service.EXCLUDED
