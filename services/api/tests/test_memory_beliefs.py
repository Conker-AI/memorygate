"""What Conker believes about the owner (R-M5): GET /runtime/memory-beliefs lists one agent's
current memories with origin, confidence, evidence counts, lineage and open conflicts, and
changes nothing.

Lineage comes from real owner messages delivered through the conversation route; other rows are
set up directly in the database. Every read goes through the HTTP API with a read key, the way
Pi calls it. Design: context/design/memory-beliefs.md.
"""

from datetime import UTC, datetime, timedelta

from app.models.conversation_receipt import ConversationReceipt
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from memory_stack import ADMIN_KEY, OWNER
from memory_stack import stack as stack

OTHER = "companion"
NOW = datetime.now(UTC)
ROUTE = "/runtime/memory-beliefs"


def add(stack, memory_id, value, agent=OWNER, minutes=0, **fields):
    fields.setdefault("created_at", NOW - timedelta(days=30) + timedelta(minutes=minutes))
    fields.setdefault("memory_type", "fact")
    with stack.sessions() as db:
        db.add(Memory(id=memory_id, agent_id=agent, text=value, **fields))
        db.commit()


def beliefs(stack, key=None, agent=OWNER, **params):
    headers = stack.read_headers(key, agent) if key else stack.read_headers(agent=agent)
    return stack.http.get(ROUTE, headers=headers, params=params)


def ok(stack, **params) -> dict:
    response = beliefs(stack, **params)
    assert response.status_code == 200, response.text
    return response.json()


def by_id(result) -> dict:
    return {item["id"]: item for item in result["items"]}


def test_stated_and_concluded_beliefs_are_told_apart(stack):
    add(stack, "m-told", "I am allergic to peanuts", source_type="user", confidence="high", minutes=3)
    add(stack, "m-said", "I live in Porto", source_type="owner_statement", minutes=2)
    add(stack, "m-heard", "Likes to run before work", source_type="automatic_listener",
        confidence="low", minutes=1)
    add(stack, "m-kept", "Prefers short answers", source_type="assistant")

    result = ok(stack)

    assert result["agent_id"] == OWNER
    assert [item["id"] for item in result["items"]] == ["m-told", "m-said", "m-heard", "m-kept"]
    items = by_id(result)
    assert {key: items["m-told"][key] for key in ("origin", "source_type", "confidence", "state", "revision")} == {
        "origin": "stated", "source_type": "user", "confidence": "high", "state": "current", "revision": 1}
    assert items["m-said"]["origin"] == "stated"
    assert items["m-heard"]["origin"] == "concluded" and items["m-heard"]["confidence"] == "low"
    assert items["m-kept"]["origin"] == "concluded"
    assert items["m-told"]["evidence"] == {"supporting": 0, "withdrawn": 0}
    assert items["m-told"]["lineage"] == [] and items["m-told"]["conflicts"] == []
    assert result["total"] == 4
    assert result["counts"] == {"stated": 2, "concluded": 2, "needs_review": 0}
    assert [item["id"] for item in ok(stack, origin="stated")["items"]] == ["m-told", "m-said"]
    concluded = ok(stack, origin="concluded")
    assert [item["id"] for item in concluded["items"]] == ["m-heard", "m-kept"]
    assert concluded["counts"] == {"stated": 0, "concluded": 2, "needs_review": 0}


def test_skills_forgotten_archived_and_other_agents_rows_are_not_beliefs(stack):
    add(stack, "m-own", "Our stand-up is at nine")
    add(stack, "m-skill", "How to file an expense report", memory_type="skill")
    add(stack, "m-archived", "An old address", status="archived")
    add(stack, "m-gone", "A forgotten message's memory")
    add(stack, "x-1", "The companion's own note", agent=OTHER)
    with stack.sessions() as db:
        db.add(ConversationReceipt(id="r-gone", agent_id=OWNER, session_id="s", message_id="msg",
                                   state="deleted", memory_id="m-gone"))
        db.commit()

    assert [item["id"] for item in ok(stack)["items"]] == ["m-own"]
    other = ok(stack, key=stack.companion_key, agent=OTHER)
    assert other["agent_id"] == OTHER and [item["id"] for item in other["items"]] == ["x-1"]


def test_needs_review_is_listed_and_filtered(stack):
    add(stack, "m-now", "Works at the bakery", minutes=1)
    add(stack, "m-doubt", "Works at the library", status="needs_review", source_type="automatic_listener",
        valid_until=NOW - timedelta(days=1))
    add(stack, "m-phase", "Training for a half marathon", memory_type="phase", minutes=2,
        review_by=NOW + timedelta(days=14))

    result = ok(stack)
    items = by_id(result)
    assert items["m-doubt"]["state"] == "needs_review" and items["m-doubt"]["valid_until"]
    assert items["m-phase"]["review_by"] and items["m-phase"]["memory_type"] == "phase"
    assert result["counts"]["needs_review"] == 1
    assert [item["id"] for item in ok(stack, state="needs_review")["items"]] == ["m-doubt"]
    assert [item["id"] for item in ok(stack, state="current")["items"]] == ["m-phase", "m-now"]
    assert [item["id"] for item in ok(stack, memory_type="phase")["items"]] == ["m-phase"]


def test_evidence_counts_and_lineage_follow_the_owner_message(stack):
    receipt = stack.say("msg_tea", "I prefer green tea in the afternoon")
    assert receipt["state"] == "admitted", receipt
    memory_id = receipt["memory_id"]

    item = by_id(ok(stack))[memory_id]
    assert item["origin"] == "stated" and item["source_type"] == "owner_statement"
    assert item["evidence"] == {"supporting": 1, "withdrawn": 0}
    assert len(item["lineage"]) == 1 and item["lineage"][0]["withdrawn"] is False
    source = item["lineage"][0]
    # The lineage link opens with the same read key the list used.
    explored = stack.runtime("explore", {"query": "explore", "object_type": source["object_type"],
                                         "object_id": source["object_id"], "operation": "connections"})
    assert explored.status_code == 200, explored.text
    assert explored.json()["object"]["id"] == source["object_id"]

    invalidated = stack.http.post(f"/runtime/evidence/{receipt['id']}/invalidate", params={"reason": "wrong"},
                                  headers=stack.admin_headers())
    assert invalidated.status_code == 200, invalidated.text

    after = by_id(ok(stack))[memory_id]
    assert after["evidence"] == {"supporting": 0, "withdrawn": 1}
    assert after["lineage"][0]["withdrawn"] is True
    assert after["state"] == "needs_review"


def test_open_conflicts_name_the_other_belief(stack):
    add(stack, "m-tea", "I drink green tea every morning", minutes=2)
    add(stack, "m-coffee", "I only drink coffee in the morning", minutes=1)
    add(stack, "m-old", "I drink cocoa in the morning", status="archived")
    add(stack, "x-1", "The companion drinks nothing", agent=OTHER)
    with stack.sessions() as db:
        db.add(MemoryConflict(id="c-open", agent_id=OWNER, memory_id="m-tea", conflicting_memory_id="m-coffee"))
        db.add(MemoryConflict(id="c-done", agent_id=OWNER, memory_id="m-tea", conflicting_memory_id="m-coffee",
                              status="resolved"))
        db.add(MemoryConflict(id="c-gone", agent_id=OWNER, memory_id="m-tea", conflicting_memory_id="m-old"))
        db.add(MemoryConflict(id="c-other", agent_id=OTHER, memory_id="x-1", conflicting_memory_id="m-tea"))
        db.commit()

    items = by_id(ok(stack))
    assert items["m-tea"]["conflicts"] == [{"conflict_id": "c-open", "memory_id": "m-coffee"}]
    assert items["m-coffee"]["conflicts"] == [{"conflict_id": "c-open", "memory_id": "m-tea"}]
    # With only one side on the page, it still names its other side.
    assert ok(stack, limit=1)["items"][0]["conflicts"] == [{"conflict_id": "c-open", "memory_id": "m-coffee"}]


def test_paging_is_stable_when_a_belief_is_corrected_between_pages(stack):
    for index in range(5):
        add(stack, f"m-{index}", f"Belief number {index}", minutes=index)

    first = ok(stack, limit=2)
    assert [item["id"] for item in first["items"]] == ["m-4", "m-3"]
    assert first["total"] == 5 and first["next_after"]
    corrected = stack.correct("fix-m-3", "m-3", 1, "Belief number three, fixed")
    assert corrected.status_code == 200, corrected.text

    second = ok(stack, limit=2, after=first["next_after"])
    assert [item["id"] for item in second["items"]] == ["m-2", "m-1"]
    third = ok(stack, limit=2, after=second["next_after"])
    assert [item["id"] for item in third["items"]] == ["m-0"] and third["next_after"] is None
    assert by_id(ok(stack))["m-3"]["revision"] == 2


def test_paging_survives_the_cursor_row_being_forgotten(stack):
    for index in range(4):
        add(stack, f"m-{index}", f"Another belief {index}", minutes=index)
    first = ok(stack, limit=2)
    assert [item["id"] for item in first["items"]] == ["m-3", "m-2"]
    assert stack.forget("forget-m-2", "m-2", 1).status_code == 200

    second = ok(stack, limit=2, after=first["next_after"])
    assert [item["id"] for item in second["items"]] == ["m-1", "m-0"]


def test_rows_with_the_same_time_page_by_id(stack):
    moment = NOW - timedelta(days=3)
    for memory_id in ("m-a", "m-b", "m-c"):
        add(stack, memory_id, f"Same moment {memory_id}", created_at=moment)
    seen, after = [], None
    while True:
        page = ok(stack, limit=1, **({"after": after} if after else {}))
        seen += [item["id"] for item in page["items"]]
        after = page["next_after"]
        if not after:
            break
    assert seen == ["m-c", "m-b", "m-a"]


def test_server_default_times_page_without_repeats(stack):
    # Rows saved without an explicit time take the database's own clock (whole seconds on SQLite).
    for memory_id in ("m-x", "m-y", "m-z"):
        with stack.sessions() as db:
            db.add(Memory(id=memory_id, agent_id=OWNER, text=f"Clock row {memory_id}", memory_type="fact"))
            db.commit()
    seen, after = [], None
    for _ in range(5):
        page = ok(stack, limit=1, **({"after": after} if after else {}))
        seen += [item["id"] for item in page["items"]]
        after = page["next_after"]
        if not after:
            break
    assert sorted(seen) == ["m-x", "m-y", "m-z"] and len(seen) == 3


def test_the_list_changes_nothing_not_even_the_recall_stamp(stack):
    add(stack, "m-1", "Keeps bees on the roof")
    with stack.sessions() as db:
        before = db.get(Memory, "m-1")
        snapshot = (before.revision, before.updated_at, before.last_recalled_at, before.status)

    ok(stack)
    ok(stack, origin="stated", state="current")

    with stack.sessions() as db:
        after = db.get(Memory, "m-1")
        assert (after.revision, after.updated_at, after.last_recalled_at, after.status) == snapshot
    assert snapshot[2] is None


def test_refused_without_a_matching_key(stack):
    add(stack, "m-1", "A private note")
    assert stack.http.get(ROUTE, headers={"X-Agent-Id": OWNER}).status_code == 401
    stack.clear_lockouts()
    assert beliefs(stack, key="mg_read_wrong_key_not_a_secret").status_code == 401
    stack.clear_lockouts()
    # A read key works only for its own agent: the companion's key cannot read the owner's rows.
    refused = beliefs(stack, key=stack.companion_key, agent=OWNER)
    assert refused.status_code in (401, 403), refused.text
    assert "A private note" not in refused.text
    stack.clear_lockouts()
    admin = stack.http.get(ROUTE, headers={"X-MemoryGate-Key": ADMIN_KEY, "X-Agent-Id": OWNER})
    assert admin.status_code == 200 and [item["id"] for item in admin.json()["items"]] == ["m-1"]
    assert ok(stack, key=stack.companion_key, agent=OTHER)["items"] == []


def test_bad_parameters_are_refused(stack):
    for params in ({"limit": 0}, {"limit": 101}, {"origin": "everyone"}, {"state": "forgotten"},
                   {"memory_type": "skill"}, {"after": ""}, {"after": "not-a-cursor"},
                   {"after": "W10"}, {"after": "x" * 601}):
        response = beliefs(stack, **params)
        assert response.status_code == 422, (params, response.text)
