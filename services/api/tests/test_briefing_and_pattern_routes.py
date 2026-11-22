"""The pre-session briefing, and observation and pattern routes beyond their basic refusals.

Every call goes through the real HTTP API. The briefing (GET /briefing/{agent_id}, admin key only)
is built from live rows: pending clarifications, active project tasks, people touched recently or
scheduled today, streaks, and recent watch memories; each section holds only that agent's rows,
and a briefing over its token budget drops sections in a fixed order. Observation and pattern
writes that name an unknown or another agent's id are refused with 422 and change nothing, even
fields of the same request that were valid. Searches narrow by every filter, never match an
entity id by a fragment, and stop at 50 results. With the vector index down an observation is
still stored and deleted, and the reply says what could not be checked.
"""

from datetime import UTC, datetime, timedelta

from app.models.entity import EntityEvent
from app.models.memory import Memory
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.services import briefing as briefing_service
from app.services.qdrant_store import OBSERVATION_COLLECTION
from memory_stack import stack as stack
from sqlalchemy import select, update

OTHER = "companion"
UNKNOWN = {"code": "unknown_ids"}
UNREACHABLE = {"status": "degraded", "component": "vector_index",
               "reason": "vector index unreachable"}


def post(stack, path, body=None, agent="owner"):
    return stack.http.post(path, headers=stack.admin_headers(agent), json=body or {})


def observe(stack, description, agent="owner", **body):
    response = post(
        stack,
        "/observation/create",
        {"signal_type": "verbal", "description": description, "hypothesis": "h", **body},
        agent,
    )
    assert response.status_code == 200, response.text
    return response.json()["observation"]


def entity(stack, entity_type, name, agent="owner", **attributes):
    body = {"entity_type": entity_type, "name": name, "attributes": attributes}
    response = post(stack, "/entity/create", body, agent)
    assert response.status_code == 200, response.text
    return response.json()["entity"]


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


def make_pattern(stack, name, agent="owner", **body):
    response = post(stack, "/pattern/create", {"pattern_name": name, **body}, agent)
    assert response.status_code == 200, response.text
    return response.json()["pattern"]


def observation_rows(stack) -> int:
    with stack.sessions() as db:
        return len(db.scalars(select(Observation.id)).all())


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


# --- observations -----------------------------------------------------------


def test_the_same_observation_twice_counts_once_more_instead_of_a_new_row(stack):
    first = observe(stack, "Hums while cooking dinner")
    response = post(stack, "/observation/create",
                    {"signal_type": "verbal", "description": "Hums while cooking dinner",
                     "hypothesis": "h"})
    body = response.json()
    assert response.status_code == 200 and body["deduplicated"] is True
    assert body["observation"]["id"] == first["id"]
    assert body["observation"]["confirmation_count"] == first["confirmation_count"] + 1
    assert observation_rows(stack) == 1

    theirs = observe(stack, "Hums while cooking dinner", agent=OTHER)
    assert theirs["id"] != first["id"] and theirs["agent_id"] == OTHER
    assert stack.get(f"/observation/{first['id']}").json()["confirmation_count"] == 1


def test_with_the_index_down_an_observation_is_stored_and_deleted_and_the_reply_says_so(stack):
    stack.index.down = True
    response = post(stack, "/observation/create",
                    {"signal_type": "verbal", "description": "Whistles on the stairs",
                     "hypothesis": "h"})
    body = response.json()
    assert response.status_code == 200
    assert (body["dedup"], body["indexing"]) == (UNREACHABLE, UNREACHABLE)
    row = body["observation"]
    assert stack.get(f"/observation/{row['id']}").json() == row

    stack.index.down = False  # the queued write lands once the index is back
    stack.drain()
    assert row["id"] in stack.index.vectors(OBSERVATION_COLLECTION)

    stack.index.down = True
    deleted = stack.http.delete(f"/observation/{row['id']}", headers=stack.admin_headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "ok", "indexing": UNREACHABLE}
    assert stack.get(f"/observation/{row['id']}").status_code == 404
    assert observation_rows(stack) == 0

    stack.index.down = False  # and so does the queued delete
    stack.drain()
    assert row["id"] not in stack.index.vectors(OBSERVATION_COLLECTION)


def test_observation_search_applies_every_filter_and_only_to_the_agent(stack):
    person = entity(stack, "human", "Noor Haddad")
    tonal = observe(stack, "Sighs at the inbox", signal_type="tonal", entity_ids=[person["id"]])
    asked = observe(stack, "Asked about moving", needs_clarification=True)
    confirmed = observe(stack, "Laughs at puns")
    post(stack, f"/observation/{confirmed['id']}/confirm", {})
    observe(stack, "Sighs at the inbox too", agent=OTHER, signal_type="tonal")

    def found(**body):
        response = post(stack, "/observation/search", body)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["results"]]

    assert found(signal_type="tonal") == [tonal["id"]]
    assert found(status="confirmed") == [confirmed["id"]]
    assert found(needs_clarification=True) == [asked["id"]]
    assert found(entity_id=person["id"]) == [tonal["id"]]
    assert found(entity_id=person["id"][:8]) == []  # a fragment of an id matches nothing
    assert found(query="SIGHS") == [tonal["id"]]
    assert found(query="sighs", status="confirmed") == []
    assert set(found()) == {tonal["id"], asked["id"], confirmed["id"]}


def test_observation_search_returns_the_fifty_newest_matches_not_fifty_scanned_rows(stack):
    # "ent_ab" contains "ent_a", so the newest rows pass the SQL narrowing and are then rejected:
    # the cap of 50 must count results, not rows looked at.
    start = datetime.now(UTC)
    with stack.sessions() as db:
        db.add_all(Observation(id=f"obs_near_{n:02}", agent_id="owner", signal_type="verbal",
                               description="near miss", hypothesis="h",
                               entity_ids_json='["ent_ab"]', observed_at=start - timedelta(minutes=n))
                   for n in range(20))
        db.add_all(Observation(id=f"obs_hit_{n:02}", agent_id="owner", signal_type="verbal",
                               description=f"note {n}", hypothesis="h",
                               entity_ids_json='["ent_a"]',
                               observed_at=start - timedelta(hours=1, minutes=n))
                   for n in range(55))
        db.commit()

    response = post(stack, "/observation/search", {"entity_id": "ent_a"})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["results"]] == [
        f"obs_hit_{n:02}" for n in range(50)
    ]


def test_session_context_exposes_matching_unconfirmed_observations_of_the_agent_only(stack):
    mine = observe(stack, "Tense about the budget", trigger_context="budget meeting")
    unrelated = observe(stack, "Likes rain", trigger_context="weather")
    theirs = observe(stack, "Their tension", agent=OTHER, trigger_context="budget meeting")

    reply = post(stack, "/observation/session-context",
                 {"session_context": "We have the budget meeting at ten."})
    assert reply.status_code == 200
    assert reply.json() == {"status": "ok", "exposed": [mine["id"]], "archived": []}
    assert stack.get(f"/observation/{mine['id']}").json()["exposure_count"] == 1
    assert stack.get(f"/observation/{unrelated['id']}").json() == unrelated
    assert stack.get(f"/observation/{theirs['id']}", agent=OTHER).json() == theirs


def test_an_observation_update_changes_every_field_it_names(stack):
    mine = observe(stack, "Cycles to work")
    linked = observe(stack, "Bought a new helmet")
    body = {"status": "confirmed", "hypothesis": "commutes by bike", "hypothesis_confidence": 0.8,
            "confirmed_by": "owner", "trigger_context": "commute", "raise_condition": "on rain",
            "needs_clarification": True, "related_observation_ids": [linked["id"]]}
    response = post(stack, f"/observation/update/{mine['id']}", body)
    assert response.status_code == 200
    row = stack.get(f"/observation/{mine['id']}").json()
    assert {key: row[key] for key in body if key != "related_observation_ids"} == {
        key: value for key, value in body.items() if key != "related_observation_ids"
    }
    assert row["related_observation_ids"] == [linked["id"]]


def test_an_observation_update_naming_an_unknown_or_foreign_observation_changes_nothing(stack):
    mine = observe(stack, "Bakes bread on Saturdays")
    theirs = observe(stack, "Their bread", agent=OTHER)
    for ids in (["obs_missing"], [theirs["id"]]):
        response = post(stack, f"/observation/update/{mine['id']}",
                        {"status": "confirmed", "hypothesis": "changed",
                         "related_observation_ids": ids})
        assert response.status_code == 422
        assert response.json()["detail"] == {**UNKNOWN, "field": "related_observation_ids",
                                             "ids": ids}
    assert stack.get(f"/observation/{mine['id']}").json() == mine


# --- patterns ---------------------------------------------------------------


def test_a_pattern_update_changes_every_field_it_names(stack):
    mine = make_pattern(stack, "tired on Mondays")
    seen = observe(stack, "Yawned at standup")
    person = entity(stack, "human", "Lea Brandt")
    body = {"description": "low energy", "observation_ids": [seen["id"]], "instance_count": 4,
            "confirmation_count": 2, "contradiction_count": 1, "confidence": 0.7,
            "interpretation": "late Sundays", "recommended_action": "lighter Monday plans",
            "applies_to_entity_ids": [person["id"]], "context_conditions": {"day": "monday"}}
    response = post(stack, f"/pattern/update/{mine['id']}", body)
    assert response.status_code == 200
    row = stack.get(f"/pattern/{mine['id']}").json()
    assert {key: row[key] for key in body} == body


def test_a_pattern_update_naming_unknown_ids_changes_nothing(stack):
    mine = make_pattern(stack, "early riser", description="up at six")
    their_obs = observe(stack, "Their note", agent=OTHER)
    their_entity = entity(stack, "human", "Their person", agent=OTHER)
    for field, ids in (("observation_ids", ["obs_missing"]),
                       ("observation_ids", [their_obs["id"]]),
                       ("applies_to_entity_ids", [their_entity["id"]])):
        response = post(stack, f"/pattern/update/{mine['id']}",
                        {"description": "changed", "confidence": 0.1, field: ids})
        assert response.status_code == 422
        assert response.json()["detail"] == {**UNKNOWN, "field": field, "ids": ids}
    assert stack.get(f"/pattern/{mine['id']}").json() == mine


def test_pattern_search_never_matches_an_id_fragment_and_combines_words_with_status(stack):
    person = entity(stack, "human", "Ravi Shah")
    make_pattern(stack, "quiet after calls", applies_to_entity_ids=[person["id"]])
    quiet_active = make_pattern(stack, "quiet mornings", status="active")
    make_pattern(stack, "loud on Fridays", status="active")
    make_pattern(stack, "quiet mornings", agent=OTHER, status="active")

    def found(**body):
        response = post(stack, "/pattern/search", body)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["results"]]

    assert found(entity_id=person["id"][:8]) == []
    assert found(query="QUIET", status="active") == [quiet_active["id"]]


def test_pattern_search_returns_the_fifty_newest_matches_not_fifty_scanned_rows(stack):
    start = datetime.now(UTC)
    with stack.sessions() as db:
        db.add_all(Pattern(id=f"pat_near_{n:02}", agent_id="owner", pattern_name="near miss",
                           applies_to_entity_ids_json='["ent_ab"]',
                           updated_at=start - timedelta(minutes=n))
                   for n in range(20))
        db.add_all(Pattern(id=f"pat_hit_{n:02}", agent_id="owner", pattern_name=f"pattern {n}",
                           applies_to_entity_ids_json='["ent_a"]',
                           updated_at=start - timedelta(hours=1, minutes=n))
                   for n in range(55))
        db.commit()

    response = post(stack, "/pattern/search", {"entity_id": "ent_a"})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["results"]] == [
        f"pat_hit_{n:02}" for n in range(50)
    ]


def test_promote_counts_only_observations_naming_the_whole_entity_id(stack):
    person = entity(stack, "human", "Mira Okafor")
    for when in ("at breakfast", "at lunch", "at dinner"):
        observe(stack, f"Mentions Mira {when}", entity_ids=[person["id"]])

    fragment = post(stack, "/pattern/promote",
                    {"pattern_name": "talks about Mira", "entity_id": person["id"][:8]})
    assert fragment.json() == {"status": "not_enough_evidence", "matched_count": 0, "required": 3}
    assert post(stack, "/pattern/search", {}).json() == {"results": []}

    promoted = post(stack, "/pattern/promote",
                    {"pattern_name": "talks about Mira", "entity_id": person["id"],
                     "query": "mentions mira"}).json()
    assert promoted["status"] == "ok"
    assert promoted["pattern"]["applies_to_entity_ids"] == [person["id"]]
