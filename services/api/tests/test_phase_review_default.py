"""A memory that becomes a phase gets a review date two weeks from now, on every path.

Creating a phase memory already sets one. Changing an existing memory to a phase (an edit, a
duplicate write that upgrades it, a listener promotion) left it empty, so the startup catch-up
later dated it from created_at, which for an old memory is already in the past.
"""

from datetime import UTC, datetime, timedelta

from app.models.memory import Memory
from app.services import runtime_pipeline
from memory_stack import OWNER
from memory_stack import stack as stack
from test_listener_evidence_erase import process, sent
from test_listener_evidence_erase import setup as setup

WINDOW = timedelta(days=15)


def _aware(value) -> datetime:
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _within_two_weeks(value) -> bool:
    now = datetime.now(UTC)
    return value is not None and now < _aware(value) <= now + WINDOW


def test_editing_an_old_memory_to_a_phase_dates_its_review_from_now(stack):
    with stack.sessions() as db:
        db.add(Memory(
            id="mem_old", agent_id=OWNER, text="Works late on Fridays", memory_type="fact",
            confidence="medium", source_type="user", tags_json="[]",
            created_at=datetime.now(UTC) - timedelta(days=60),
        ))
        db.commit()

    response = stack.http.patch("/memory/mem_old", headers=stack.admin_headers(), json={"memory_type": "phase"})

    assert response.status_code == 200, response.text
    assert _within_two_weeks(response.json()["memory"]["review_by"])


def test_a_duplicate_write_that_upgrades_to_a_phase_sets_its_review_date(stack):
    text = "I prefer a quiet desk while I train for the spring half marathon"
    first = stack.write(text, memory_type="context", confidence="low")

    again = stack.write(text, memory_type="phase", confidence="high")

    assert again["duplicate_of"] == first["id"] and again["upgraded"] is True
    with stack.sessions() as db:
        assert _within_two_weeks(db.get(Memory, first["id"]).review_by)


def test_a_listener_promotion_to_a_phase_sets_its_review_date(setup, monkeypatch):
    http, sessions, _engine, _removed = setup
    monkeypatch.setattr(runtime_pipeline, "classify_memory", lambda content, source_type: {
        "memory_type": "phase", "confidence": "medium", "summary": content[:280],
    })

    result = process(sessions, sent(http))

    with sessions() as db:
        assert _within_two_weeks(db.get(Memory, result["memory_id"]).review_by)
