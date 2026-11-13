import pytest
from app.core.db import Base
from app.models.conversation_receipt import ConversationReceipt
from app.models.deletion_receipt import DeletionReceipt, RecoveryHold
from app.models.entity import Entity
from app.models.memory import Memory
from app.models.observation import Observation
from app.services import conversation_memory
from app.services import deletion_recovery as recovery
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def test_real_local_vectors_are_deleted_and_read_back(target):
    from uuid import uuid4

    from app.models.deletion_receipt import RecoveryVerification
    from qdrant_client import QdrantClient, models
    identity, keep = str(uuid4()), str(uuid4())
    client = QdrantClient(":memory:")
    collections = {kind: "recovery_" + kind for kind in ("memory", "entity", "observation")}
    try:
        for name in collections.values():
            client.create_collection(name, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
            client.upsert(name, points=[models.PointStruct(id=key, vector=[1.0, 0.0]) for key in (identity, keep)])
        recovery.replay([("owner", kind, identity) for kind in collections], target)
        result = recovery.reconcile_indexes(target, client, collections)
        assert result["pointCount"] == 3 and result["indexCleanupVerified"]
        assert result == recovery.reconcile_indexes(target, client, collections)
        for name in collections.values():
            assert [str(row.id) for row in client.retrieve(name, ids=[identity, keep])] == [keep]
        with target() as db:
            assert db.get(RecoveryVerification, "vector-deletions").evidence_digest == result["evidenceDigest"]
            with pytest.raises(RuntimeError):
                recovery.assert_not_held(db)
    finally:
        client.close()


def test_index_failure_never_records_success(target):
    from types import SimpleNamespace

    from app.models.deletion_receipt import RecoveryVerification
    collections = {kind: kind for kind in ("memory", "entity", "observation")}
    class FailingClient:
        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in collections])
        def delete(self, **kwargs):
            pass
        def retrieve(self, **kwargs):
            return [object()]
    recovery.replay([("owner", "memory", "still-present")], target)
    with pytest.raises(ValueError, match="retain the hold"):
        recovery.reconcile_indexes(target, FailingClient(), collections)
    with target() as db:
        assert db.get(RecoveryVerification, "vector-deletions") is None
        assert db.get(RecoveryHold, "deletion-replay") is not None


@pytest.fixture
def target(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'restored.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    yield sessions
    engine.dispose()


def test_replay_and_replay_again_retain_index_cleanup_and_hold(target):
    with target() as db:
        db.add_all([Memory(id="memory", agent_id="owner", text="erase"),
                    Entity(id="entity", agent_id="owner", entity_type="person", name="erase"),
                    Observation(id="observation", agent_id="owner", signal_type="verbal", description="erase"),
                    Memory(id="keep", agent_id="other", text="keep")])
        db.commit()
    evidence = [("owner", kind, kind) for kind in ("memory", "entity", "observation")]
    first = recovery.replay(evidence, target)
    assert first == recovery.replay(evidence, target)
    assert len(first["indexDeletes"]) == 3 and first["recoveryHeld"]
    with target() as db:
        assert db.get(Memory, "memory") is None
        assert db.get(Memory, "keep").text == "keep"
        assert len(list(db.scalars(select(DeletionReceipt)))) == 3
        with pytest.raises(RuntimeError, match="held"):
            recovery.assert_not_held(db)


def test_namespace_conflict_keeps_hold_and_preserves_foreign_data(target):
    with target() as db:
        db.add(Memory(id="foreign", agent_id="other", text="keep"))
        db.commit()
    with pytest.raises(ValueError, match="namespace"):
        recovery.replay([("owner", "memory", "foreign")], target)
    with target() as db:
        assert db.get(Memory, "foreign").text == "keep"
        assert db.get(RecoveryHold, "deletion-replay") is not None


def test_conversation_replay_prevents_late_upload(target, monkeypatch):
    monkeypatch.setattr(conversation_memory, "SessionLocal", target)
    payload = {"session_id": "ses_one", "content": "I prefer to train before school", "created_at": 1770000000.0}
    original = conversation_memory.ingest("owner", "msg_one", payload)
    result = recovery.replay([("owner", "conversation", "msg_one")], target)
    assert {"kind": "memory", "id": original["memory_id"]} in result["indexDeletes"]
    assert conversation_memory.ingest("owner", "msg_one", payload)["state"] == "deleted"
    with target() as db:
        assert db.get(Memory, original["memory_id"]) is None
        assert db.get(ConversationReceipt, original["id"]).index_pending == "delete"
        assert ("owner", "conversation", "msg_one") in recovery.evidence(db)


def test_memory_replay_erases_revisions_conflicts_and_audit_text(target):
    import json

    from app.models.audit import MemoryAudit
    from app.models.memory_conflict import MemoryConflict
    from app.models.memory_revision import MemoryRevision
    secret = json.dumps({"text": "my PIN is 4321"})
    with target() as db:
        db.add_all([Memory(id="m1", agent_id="owner", text="my PIN is 4321"),
                    MemoryRevision(memory_id="m1", changed_by="system", snapshot_json=secret),
                    MemoryAudit(action="write", memory_id="m1", payload_json=secret),
                    MemoryConflict(agent_id="owner", memory_id="m2", conflicting_memory_id="m1"),
                    MemoryRevision(id="keep-revision", memory_id="keep", snapshot_json="{}")])
        db.commit()

    def assert_clean():
        with target() as db:
            assert db.get(Memory, "m1") is None
            assert not db.scalars(select(MemoryRevision).where(MemoryRevision.memory_id == "m1")).all()
            assert db.get(MemoryRevision, "keep-revision") is not None
            assert not db.scalars(select(MemoryConflict).where(
                (MemoryConflict.memory_id == "m1") | (MemoryConflict.conflicting_memory_id == "m1"))).all()
            assert not [row for row in db.scalars(select(MemoryAudit)) if "4321" in row.payload_json]

    recovery.replay([("owner", "memory", "m1")], target)
    assert_clean()
    # A restarted replay, with the row already gone, still removes a leftover copy.
    with target() as db:
        db.add(MemoryRevision(memory_id="m1", snapshot_json=secret))
        db.commit()
    recovery.replay([("owner", "memory", "m1")], target)
    assert_clean()
