"""The memory and entity lists page: older rows past the first page can still be listed."""

from datetime import datetime, timedelta

from app.models.entity import Entity
from app.models.memory import Memory
from memory_stack import OWNER
from memory_stack import stack as stack

START = datetime(2026, 1, 1)


def test_older_memories_are_reachable_through_offset(stack):
    with stack.sessions() as db:
        db.add_all([Memory(id=f"m{n:03d}", agent_id=OWNER, text=f"fact {n}",
                           created_at=START + timedelta(minutes=n)) for n in range(130)])
        db.commit()

    first = [row["id"] for row in stack.get("/memory").json()]
    rest = [row["id"] for row in stack.get("/memory?offset=100").json()]
    assert len(first) == 100 and first[0] == "m129"
    assert rest == [f"m{n:03d}" for n in range(29, -1, -1)]  # the 30 oldest, none twice
    assert [row["id"] for row in stack.get("/memory?limit=2&offset=1").json()] == ["m128", "m127"]
    assert stack.get("/memory?limit=0").status_code == 422


def test_older_entities_are_reachable_through_offset(stack):
    with stack.sessions() as db:
        db.add_all([Entity(id=f"e{n:03d}", agent_id=OWNER, entity_type="person", name=f"Person {n}",
                           updated_at=START + timedelta(minutes=n)) for n in range(105)])
        db.commit()

    seen = [row["id"] for row in stack.get("/entity").json()] + [
        row["id"] for row in stack.get("/entity?offset=100").json()]
    assert seen == [f"e{n:03d}" for n in range(104, -1, -1)]
