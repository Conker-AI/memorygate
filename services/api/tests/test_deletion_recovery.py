import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models.deletion_receipt import DeletionReceipt, RecoveryHold
from app.models.memory import Memory
from app.models.entity import Entity
from app.models.observation import Observation
from app.models.conversation_receipt import ConversationReceipt
from app.services import deletion_recovery as recovery
from app.services import conversation_memory


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
