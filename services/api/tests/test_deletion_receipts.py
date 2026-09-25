import json

from app.models.deletion_receipt import DeletionReceipt
from app.models.memory import Memory
from app.services import backup_service
from sqlalchemy import select
from test_memory_revisions import setup  # noqa: F401


def test_memory_delete_records_only_identity_and_backup_retains_it(request, tmp_path, monkeypatch):
    http, sessions = request.getfixturevalue("setup")
    assert http.delete("/memory/one?expected_revision=2").status_code == 409
    with sessions() as db:
        assert list(db.scalars(select(DeletionReceipt))) == []
    assert http.delete("/memory/one?expected_revision=1").status_code == 200
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path))
    with sessions() as db:
        row = db.get(DeletionReceipt, ("owner", "memory", "one"))
        assert row is not None and row.deleted_at is not None
        assert db.get(Memory, "one") is None
        result = backup_service.create_backup(db)
    payload = json.loads((tmp_path / result["filename"]).read_text())
    receipts = payload["tables"]["deletion_receipts"]
    assert len(receipts) == 1
    assert set(receipts[0]) == {"agent_id", "object_kind", "object_id", "deleted_at"}
    assert "Original" not in json.dumps(receipts)


def test_delete_receipt_failure_rolls_back_memory(request, monkeypatch):
    http, sessions = request.getfixturevalue("setup")
    import app.models.deletion_receipt as receipts
    def fail(*args):
        raise RuntimeError("injected ledger failure")
    monkeypatch.setattr(receipts, "record", fail)
    assert http.delete("/memory/one?expected_revision=1").status_code == 500
    with sessions() as db:
        assert db.get(Memory, "one") is not None
        assert list(db.scalars(select(DeletionReceipt))) == []
