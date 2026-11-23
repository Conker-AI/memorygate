"""The pre-session briefing (GET /briefing/{agent_id}, admin key only).

Every call goes through the real HTTP API. The briefing is built from live rows: pending
clarifications, active project tasks, people touched recently or scheduled today, streaks, and
recent watch memories; each section holds only that agent's rows, and a briefing over its token
budget drops sections in a fixed order.
"""

from datetime import UTC, datetime, timedelta

from app.models.entity import EntityEvent
from app.models.memory import Memory
from app.services import briefing as briefing_service
from memory_stack import stack as stack
from route_helpers import OTHER, entity, observe, post
from sqlalchemy import update


def event(stack, entity_id, description, agent="owner", event_type="note", days_ago=0):
    body = {"entity_id": entity_id, "event_type": event_type, "description": description}
    response = post(stack, "/entity/event", body, agent)
    assert response.status_code == 200, response.text
    event_id = response.json()["event"]["id"]
    if days_ago:
        when = datetime.now(UTC) - timedelta(days=days_ago)
        with stack.sessions() as db:
            db.execute(update(EntityEvent).where(EntityEvent.id == event_id).values(created_at=when))
            db.commit()
    return event_id


def watch(stack, value, agent="owner", summary="", days_ago=0, memory_type="watch"):
    with stack.sessions() as db:
        db.add(Memory(
            agent_id=agent, text=value, summary=summary, memory_type=memory_type, status="active",
            created_at=datetime.now(UTC) - timedelta(days=days_ago),
        ))
        db.commit()


def briefing(stack, agent="owner"):
    response = stack.get(f"/briefing/{agent}")
    assert response.status_code == 200, response.text
    return response.json()



# --- briefing ---------------------------------------------------------------


def test_the_briefing_needs_the_admin_key(stack):
    assert stack.http.get("/briefing/owner").status_code == 401
    response = stack.http.get("/briefing/owner", headers=stack.read_headers())
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or invalid X-MemoryGate-Key"


def test_an_agent_with_nothing_stored_gets_an_empty_briefing(stack):
    assert briefing(stack) == {
        "emotional_state": None,
        "mood_summary": None,
        "active_streaks": [],
        "pending_clarifications": [],
        "active_tasks": [],
        "people_relevant": [],
        "watch_flags": [],
    }


def test_pending_clarifications_are_the_two_surest_unconfirmed_questions_of_the_agent(stack):
    observe(stack, "Mentioned a trip", needs_clarification=True,
                  hypothesis_confidence=0.2, raise_condition="when travel comes up")
    high = observe(stack, "Said the move is off", needs_clarification=True,
                   hypothesis_confidence=0.9, raise_condition="when housing comes up")
    middle = observe(stack, "Hinted at a new job", needs_clarification=True,
                     hypothesis_confidence=0.6)
    answered = observe(stack, "Asked about the dentist", needs_clarification=True,
                       hypothesis_confidence=0.99)
    confirm = post(stack, f"/observation/{answered['id']}/confirm", {"confirmed_by": "owner"})
    assert confirm.status_code == 200, confirm.text
    observe(stack, "Not a question", hypothesis_confidence=0.95)
    observe(stack, "Their own question", agent=OTHER, needs_clarification=True,
            hypothesis_confidence=1.0)

    assert briefing(stack)["pending_clarifications"] == [
        {"id": high["id"], "what": "Said the move is off", "trigger_condition": "when housing comes up"},
        {"id": middle["id"], "what": "Hinted at a new job", "trigger_condition": ""},
    ]
    other = briefing(stack, OTHER)["pending_clarifications"]
    assert [item["what"] for item in other] == ["Their own question"]


def test_active_tasks_are_open_projects_touched_in_the_last_three_weeks(stack):
    stuck = entity(stack, "project", "Garden shed", status="blocked", sessions_stuck=4)
    fresh = entity(stack, "project", "Tax return", status="in progress", sessions_stuck=1)
    done = entity(stack, "project", "Old website", status="completed", sessions_stuck=9)
    stale = entity(stack, "project", "Novel draft", status="paused", sessions_stuck=7)
    entity(stack, "project", "Untouched idea", sessions_stuck=8)
    theirs = entity(stack, "project", "Their roadmap", agent=OTHER, sessions_stuck=10)
    event(stack, stuck["id"], "ordered timber")
    event(stack, fresh["id"], "found receipts")
    event(stack, done["id"], "launched")
    event(stack, stale["id"], "wrote a chapter", days_ago=30)
    event(stack, theirs["id"], "planned", agent=OTHER)

    assert briefing(stack)["active_tasks"] == [
        {"entity_id": stuck["id"], "name": "Garden shed", "status": "blocked", "sessions_stuck": 4},
        {"entity_id": fresh["id"], "name": "Tax return", "status": "in progress", "sessions_stuck": 1},
    ]
    assert [task["name"] for task in briefing(stack, OTHER)["active_tasks"]] == ["Their roadmap"]


def test_people_are_those_touched_this_week_or_scheduled_today(stack, monkeypatch):
    now = datetime.now(UTC)  # one clock for "today", so midnight between steps cannot move it
    monkeypatch.setattr(briefing_service, "_now", lambda: now)
    today = now.date().isoformat()
    sam = entity(stack, "human", "Sam Reyes", warmth_level=0.9)
    noted = entity(stack, "human", "Ida Lund", note="call back about the lease")
    scheduled = entity(stack, "human", "Theo Marsh", scheduled_for=today)
    distant = entity(stack, "human", "Uma Falk")
    entity(stack, "human", "Never mentioned")
    theirs = entity(stack, "human", "Their friend", agent=OTHER)
    event(stack, sam["id"], "lent a ladder")
    event(stack, noted["id"], "texted")
    event(stack, distant["id"], "met at a wedding", days_ago=10)
    event(stack, theirs["id"], "met", agent=OTHER)

    people = {person["name"]: person for person in briefing(stack)["people_relevant"]}
    assert people == {
        "Sam Reyes": {"entity_id": sam["id"], "name": "Sam Reyes", "warmth_level": 0.9,
                      "note": "lent a ladder"},
        "Ida Lund": {"entity_id": noted["id"], "name": "Ida Lund", "warmth_level": 0.5,
                     "note": "call back about the lease"},
        "Theo Marsh": {"entity_id": scheduled["id"], "name": "Theo Marsh", "warmth_level": 0.5,
                       "note": ""},
    }
    assert [p["name"] for p in briefing(stack, OTHER)["people_relevant"]] == ["Their friend"]


def test_streaks_count_repeated_streak_events_of_the_last_month(stack):
    # Each habit logs its streak events under its own name, so the expected streak name is the
    # same whether a streak is keyed on the habit or on the event description.
    run = entity(stack, "habit", "Morning run")
    for _ in range(3):
        event(stack, run["id"], "Morning run", event_type="streak")
    event(stack, run["id"], "Morning run")  # not a streak event
    stretch = entity(stack, "habit", "Stretching")
    event(stack, stretch["id"], "Stretching", event_type="streak")  # once is not a streak
    swim = entity(stack, "habit", "Swimming")
    event(stack, swim["id"], "Swimming", event_type="streak")
    event(stack, swim["id"], "Swimming", event_type="streak", days_ago=40)  # outside the window
    theirs = entity(stack, "habit", "Their habit", agent=OTHER)
    for _ in range(2):
        event(stack, theirs["id"], "Their habit", agent=OTHER, event_type="streak")

    assert briefing(stack)["active_streaks"] == [{"name": "Morning run", "count": 3}]
    assert briefing(stack, OTHER)["active_streaks"] == [{"name": "Their habit", "count": 2}]


def test_watch_flags_are_the_five_newest_recent_watch_memories(stack):
    for day in range(6):
        watch(stack, f"watch item {day}", days_ago=day + 1)
    watch(stack, "x" * 200, summary="Sleep is short", days_ago=0.5)
    watch(stack, "a" * 200, days_ago=0)  # no summary: the text, cut to 120 characters
    watch(stack, "a plain fact", memory_type="fact")
    watch(stack, "their watch item", agent=OTHER)

    assert briefing(stack)["watch_flags"] == [
        "a" * 120, "Sleep is short", "watch item 0", "watch item 1", "watch item 2",
    ]
    assert briefing(stack, OTHER)["watch_flags"] == ["their watch item"]


def test_watch_flags_hold_only_the_last_two_weeks(stack):
    watch(stack, "inside the window", days_ago=13)
    watch(stack, "outside the window", days_ago=20)

    assert briefing(stack)["watch_flags"] == ["inside the window"]


def test_an_overflowing_briefing_drops_watch_flags_first(stack):
    shed = entity(stack, "project", "Shed", status="blocked", sessions_stuck=2)
    event(stack, shed["id"], "ordered timber")
    taxes = entity(stack, "project", "Taxes", sessions_stuck=1)
    event(stack, taxes["id"], "found receipts")
    sam = entity(stack, "human", "Sam Reyes")
    event(stack, sam["id"], "lent a ladder")
    for word in ("first", "second"):
        observe(stack, f"Asked a {word} thing", needs_clarification=True, signal_type=word)
    for day in range(5):
        watch(stack, "a long watch memory", summary=f"{day} " + "w" * 300)

    result = briefing(stack)
    assert result["watch_flags"] == []
    assert [task["name"] for task in result["active_tasks"]] == ["Shed", "Taxes"]
    assert [person["name"] for person in result["people_relevant"]] == ["Sam Reyes"]
    assert sorted(item["what"] for item in result["pending_clarifications"]) == [
        "Asked a first thing", "Asked a second thing",
    ]


def test_people_go_second_when_dropping_watch_flags_is_not_enough(stack):
    for name in ("Shed", "Taxes"):
        project = entity(stack, "project", name)
        event(stack, project["id"], "worked on it")
    for number in range(3):
        person = entity(stack, "human", f"Person {number} " + "n" * 400)
        event(stack, person["id"], "met")
    watch(stack, "keep an eye on sleep")

    result = briefing(stack)
    assert result["watch_flags"] == [] and result["people_relevant"] == []
    assert sorted(task["name"] for task in result["active_tasks"]) == ["Shed", "Taxes"]


def test_tasks_beyond_the_most_stuck_go_third_and_both_questions_stay(stack):
    for number in range(3):
        project = entity(stack, "project", f"Project {number} " + "p" * 400,
                         sessions_stuck=number)
        event(stack, project["id"], "worked on it")
    for word in ("first", "second"):
        observe(stack, f"Asked a {word} thing", needs_clarification=True, signal_type=word)

    result = briefing(stack)
    assert [task["sessions_stuck"] for task in result["active_tasks"]] == [2]
    assert len(result["pending_clarifications"]) == 2


def test_a_briefing_far_over_budget_keeps_one_task_and_one_question(stack):
    for number in range(4):
        observe(stack, f"Question {number} " + "q" * 700, needs_clarification=True,
                signal_type=f"kind{number}", hypothesis_confidence=number / 10)
    for number in range(6):
        project = entity(stack, "project", f"Project {number} " + "p" * 120,
                         sessions_stuck=number)
        event(stack, project["id"], "worked on it")
    for number in range(4):
        person = entity(stack, "human", f"Person {number} " + "n" * 100)
        event(stack, person["id"], "met")
    watch(stack, "keep an eye on sleep")

    result = briefing(stack)
    assert result["watch_flags"] == [] and result["people_relevant"] == []
    assert [task["sessions_stuck"] for task in result["active_tasks"]] == [5]
    assert [item["what"][:10] for item in result["pending_clarifications"]] == ["Question 3"]
