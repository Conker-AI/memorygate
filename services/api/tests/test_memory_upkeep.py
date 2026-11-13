"""Memory upkeep candidates (R-M4): GET /runtime/memory-upkeep lists what could be merged,
resolved, retired or checked, each with ids and a reason, and changes nothing.

Rows are set up directly in the database (writes dedupe exact text, and conflicts come from the
truth check); every read goes through the real HTTP API with a read key, the way Pi calls it.
"""

from datetime import UTC, datetime, timedelta

from app.models.conversation_receipt import ConversationReceipt
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from memory_stack import ADMIN_KEY, OWNER
from memory_stack import stack as stack
from sqlalchemy import text

OTHER = "companion"
NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=200)


def add(stack, memory_id, value, agent=OWNER, **fields):
    fields.setdefault("created_at", OLD)
    with stack.sessions() as db:
        db.add(Memory(id=memory_id, agent_id=agent, text=value, **fields))
        db.commit()


def upkeep(stack, key=None, agent=OWNER, **params):
    headers = stack.read_headers(key, agent) if key else stack.read_headers(agent=agent)
    return stack.http.get("/runtime/memory-upkeep", headers=headers, params=params)


def ids(result, kind):
    return [item["ids"] for item in result[kind]["items"]]


def flat(result):
    return {i for kind in ("exact_duplicates", "near_duplicates", "contradictions", "expired", "not_recalled")
            for item in result[kind]["items"] for i in item["ids"]}


def test_exact_and_near_duplicates_are_listed_with_a_reason(stack):
    add(stack, "m-a", "I take the early train to work on Mondays", last_recalled_at=NOW)
    add(stack, "m-b", "  i take the EARLY train to work on   mondays", last_recalled_at=NOW)
    add(stack, "m-c", "My sister Anna lives in Lisbon with her two cats", last_recalled_at=NOW)
    add(stack, "m-d", "My sister Anna lives in Lisbon with her cats", last_recalled_at=NOW)
    add(stack, "m-e", "The garage code changes every spring", last_recalled_at=NOW)

    response = upkeep(stack)

    assert response.status_code == 200, response.text
    result = response.json()
    assert ids(result, "exact_duplicates") == [["m-a", "m-b"]]
    assert result["exact_duplicates"]["items"][0]["reason"] == "same text once case and spacing are ignored"
    assert ids(result, "near_duplicates") == [["m-c", "m-d"]]
    assert "% of their words are the same" in result["near_duplicates"]["items"][0]["reason"]
    assert result["memories_checked"] == 5 and result["truncated"] is False


def test_open_conflicts_expired_and_unused_memories_are_listed(stack):
    add(stack, "m-tea", "I drink green tea every morning", last_recalled_at=NOW)
    add(stack, "m-coffee", "I never drink anything but coffee in the morning", last_recalled_at=NOW)
    add(stack, "m-lease", "The flat lease runs through the summer", last_recalled_at=NOW,
        valid_until=NOW - timedelta(days=3))
    add(stack, "m-phase", "Training for the half marathon", last_recalled_at=NOW,
        review_by=NOW - timedelta(days=1))
    add(stack, "m-never", "The spare key is under the blue pot")
    add(stack, "m-stale", "Parking is on level three", last_recalled_at=NOW - timedelta(days=120))
    add(stack, "m-new", "The dentist moved to Elm Street", created_at=NOW - timedelta(days=2))
    with stack.sessions() as db:
        db.add(MemoryConflict(id="c-open", agent_id=OWNER, memory_id="m-tea",
                              conflicting_memory_id="m-coffee", reason="morning drink differs"))
        db.add(MemoryConflict(id="c-done", agent_id=OWNER, memory_id="m-tea",
                              conflicting_memory_id="m-never", status="resolved"))
        db.commit()

    result = upkeep(stack, unused_days=90).json()

    assert result["contradictions"]["items"] == [
        {"ids": ["m-tea", "m-coffee"], "conflict_id": "c-open", "reason": "morning drink differs"}]
    expired = {item["ids"][0]: item["reason"] for item in result["expired"]["items"]}
    assert expired["m-lease"].startswith("valid until ")
    assert expired["m-phase"].startswith("review was due ")
    unused = {item["ids"][0]: item["reason"] for item in result["not_recalled"]["items"]}
    assert unused["m-never"] == "saved over 90 days ago and never recalled"
    assert unused["m-stale"].startswith("last recalled ")
    assert "m-new" not in unused and "m-tea" not in unused


def test_another_agents_and_forgotten_or_archived_rows_never_appear(stack):
    add(stack, "m-own", "Our team stand-up is at nine", last_recalled_at=NOW)
    add(stack, "m-own2", "Our team stand-up is at nine")
    add(stack, "x-1", "Our team stand-up is at nine", agent=OTHER)
    add(stack, "x-2", "Our team stand-up is at nine", agent=OTHER, valid_until=NOW - timedelta(days=9))
    add(stack, "m-archived", "Our team stand-up is at nine", status="archived")
    add(stack, "m-gone", "Our team stand-up is at nine")
    with stack.sessions() as db:
        db.add(ConversationReceipt(id="r-gone", agent_id=OWNER, session_id="s", message_id="msg",
                                   state="deleted", memory_id="m-gone"))
        db.add(MemoryConflict(agent_id=OTHER, memory_id="x-1", conflicting_memory_id="x-2"))
        db.add(MemoryConflict(agent_id=OWNER, memory_id="m-own", conflicting_memory_id="x-1"))
        db.commit()

    result = upkeep(stack).json()

    assert result["agent_id"] == OWNER
    assert ids(result, "exact_duplicates") == [["m-own", "m-own2"]]
    assert flat(result) == {"m-own", "m-own2"}
    assert result["contradictions"]["total"] == 0

    other = upkeep(stack, key=stack.companion_key, agent=OTHER).json()
    assert other["agent_id"] == OTHER and flat(other) == {"x-1", "x-2"}


def test_refused_without_a_matching_key(stack):
    add(stack, "m-1", "A private note")
    assert stack.http.get("/runtime/memory-upkeep", headers={"X-Agent-Id": OWNER}).status_code == 401
    stack.clear_lockouts()
    assert upkeep(stack, key="mg_read_wrong_key_not_a_secret").status_code == 401
    stack.clear_lockouts()
    # A read key works only for its own agent: the companion's key cannot read the owner's rows.
    refused = upkeep(stack, key=stack.companion_key, agent=OWNER)
    assert refused.status_code in (401, 403), refused.text
    assert "A private note" not in refused.text
    admin = stack.http.get("/runtime/memory-upkeep",
                           headers={"X-MemoryGate-Key": ADMIN_KEY, "X-Agent-Id": OWNER})
    assert admin.status_code == 200 and admin.json()["agent_id"] == OWNER


def test_bad_parameters_are_refused(stack):
    for params in ({"unused_days": 0}, {"limit": 0}, {"limit": 501}, {"unused_days": "soon"}):
        assert upkeep(stack, **params).status_code == 422, params


def test_limit_caps_each_kind_and_total_counts_all(stack):
    for number in range(4):
        add(stack, f"m-{number}", f"Unused note number {number} about the {'abcd'[number]}oat")

    result = upkeep(stack, limit=2).json()

    assert len(result["not_recalled"]["items"]) == 2
    assert result["not_recalled"]["total"] == 4


def test_the_route_changes_nothing(stack):
    add(stack, "m-a", "Same words here twice", valid_until=NOW - timedelta(days=1))
    add(stack, "m-b", "Same words here twice")
    with stack.sessions() as db:
        db.add(MemoryConflict(agent_id=OWNER, memory_id="m-a", conflicting_memory_id="m-b"))
        db.commit()

    def snapshot():
        with stack.engine.connect() as db:
            return {table: [tuple(row) for row in db.execute(text(f'SELECT * FROM "{table}" ORDER BY 1'))]
                    for table in ("memories", "memory_conflicts", "memory_audit", "memory_revisions")}

    before = snapshot()
    assert upkeep(stack).status_code == 200
    assert upkeep(stack, key=stack.companion_key, agent=OTHER).status_code == 200
    assert snapshot() == before


def test_recall_stamps_last_recalled_without_editing_the_memory(stack):
    written = stack.write("I always walk home the long way when it is grey", source_type="user")
    memory_id = written["id"]
    with stack.sessions() as db:
        before = db.get(Memory, memory_id)
        revision, updated = before.revision, before.updated_at
        assert before.last_recalled_at is None

    context = stack.context("how do I walk home when it is grey")

    assert memory_id in [item["id"] for item in context["memories"]]
    with stack.sessions() as db:
        after = db.get(Memory, memory_id)
        assert after.last_recalled_at is not None
        assert (after.revision, after.updated_at) == (revision, updated)


def test_a_failed_stamp_never_fails_recall(stack, monkeypatch):
    from app.services import memory_upkeep
    from sqlalchemy.exc import OperationalError

    memory_id = stack.write("I always walk home the long way when it is grey", source_type="user")["id"]

    def locked(*_args, **_kwargs):
        raise OperationalError("UPDATE memories", {}, Exception("deadlock detected"))

    monkeypatch.setattr(memory_upkeep, "update", locked)
    context = stack.context("how do I walk home when it is grey")

    assert memory_id in [item["id"] for item in context["memories"]]
    with stack.sessions() as db:
        assert db.get(Memory, memory_id).last_recalled_at is None


def test_recall_restamps_at_most_once_an_hour(stack):
    from app.services import memory_upkeep

    add(stack, "m-1", "A note", last_recalled_at=NOW - timedelta(minutes=10))
    add(stack, "m-2", "Another note", last_recalled_at=NOW - timedelta(hours=3))
    with stack.sessions() as db:
        memory_upkeep.stamp_recalled(db, ["m-1", "m-2", "m-1"], now=NOW)
    with stack.sessions() as db:
        recent, old = db.get(Memory, "m-1").last_recalled_at, db.get(Memory, "m-2").last_recalled_at
    assert recent.replace(tzinfo=UTC) == NOW - timedelta(minutes=10)
    assert old.replace(tzinfo=UTC) == NOW
