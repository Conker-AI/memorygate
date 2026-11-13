"""A write matches only an active memory as its duplicate, however old that memory is."""

from datetime import UTC, datetime, timedelta

from app.models.memory import Memory
from memory_stack import stack as stack

TEXT = "I prefer the release checklist printed on paper"


def test_a_write_equal_to_a_memory_under_review_makes_an_active_memory(stack):
    with stack.sessions() as db:
        db.add(Memory(id="m_review", agent_id="owner", text=TEXT, status="needs_review"))
        db.commit()

    written = stack.write(TEXT)

    assert written.get("duplicate_of") is None and written["id"] != "m_review"
    with stack.sessions() as db:
        assert db.get(Memory, written["id"]).status == "active"


def test_an_old_active_memory_is_still_found_as_the_duplicate(stack):
    first = stack.write(TEXT)["id"]
    with stack.sessions() as db:
        later = datetime.now(UTC) + timedelta(days=1)
        db.add_all([Memory(id=f"m{n:03d}", agent_id="owner", text=f"Filler memory number {n} about lunch",
                           created_at=later + timedelta(seconds=n)) for n in range(120)])
        db.commit()

    again = stack.write("  " + TEXT.upper() + " ")

    assert again["duplicate_of"] == first
