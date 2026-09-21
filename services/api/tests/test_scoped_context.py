import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.db import Base
from app.models.memory import Memory
from app.models.conversation_receipt import ConversationReceipt
from app.routes import runtime
from app.schemas.runtime import AgentContextRequest

@pytest.fixture
def db(tmp_path, monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path / 'scope.db'}")
    Base.metadata.create_all(engine)
    def forbidden(*args, **kwargs):
        raise AssertionError("Scoped retrieval must not use broad helpers")
    monkeypatch.setattr(runtime,"semantic_status",forbidden)
    monkeypatch.setattr(runtime,"build_briefing",forbidden)
    with sessionmaker(engine)() as session:
        for identity, agent, status in [("selected","owner","active"),("other","owner","active"),("foreign","another","active"),("inactive","owner","needs_review")]:
            session.add(Memory(id=identity,agent_id=agent,text=identity,status=status))
        session.add(ConversationReceipt(id="receipt1",agent_id="owner",message_id="msg-one",session_id="session-one",memory_id="selected",state="admitted"))
        session.add(ConversationReceipt(id="receipt2",agent_id="owner",message_id="msg-two",session_id="session-two",memory_id="other",state="admitted"))
        session.commit()
        yield session
    engine.dispose()

def test_selected_never_widens_to_other_memory_or_helpers(db):
    body=AgentContextRequest(query="anything",scope="selected",memory_ids=["selected","foreign","inactive"],include_evidence=True)
    result=runtime._build_context(db,body,"owner")
    assert [item["id"] for item in result["memories"]]==["selected"]
    assert result["entities"]==result["episodes"]==result["evidence"]==[]
    assert result["briefing"]=={}
    assert result["memories"][0]["citations"][0]["session_id"]=="session-one"

def test_conversation_scope_and_none(db):
    body=AgentContextRequest(query="anything",scope="conversation",session_id="session-two")
    result=runtime._build_context(db,body,"owner")
    assert [item["id"] for item in result["memories"]]==["other"]
    assert result["memories"][0]["citations"][0]["session_id"]=="session-two"
    assert runtime._build_context(db,body,"another")["memories"]==[]
    assert runtime._build_context(db,AgentContextRequest(query="x",scope="none"),"owner")["memories"]==[]

@pytest.mark.parametrize("extra",[
    {"scope":"selected"},{"scope":"conversation"},{"scope":"none","memory_ids":["selected"]},
    {"scope":"selected","memory_ids":["x","x"]},{"scope":"selected","memory_ids":[" "]},
    {"scope":"all","session_id":"session-one"},{"memoryIds":["selected"]},
])
def test_ambiguous_scope_rejected(extra):
    with pytest.raises(ValidationError):
        AgentContextRequest(query="x",**extra)
