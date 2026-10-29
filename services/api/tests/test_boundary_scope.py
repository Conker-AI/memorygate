"""Scoped retrieval keeps to its scope, and a read key keeps to its namespace.

Pi reads with a read key bound to one agent. Scope `conversation` returns only memory
admitted from that chat, `selected` only the listed ids, `none` nothing, and `all`
everything active in the key's namespace - never another agent's, whichever path
(semantic or lexical) would find it. Every call goes through the real HTTP API.
"""

import json

import pytest
from memory_stack import READ_KEY
from memory_stack import stack as stack

TEA = "I prefer green tea in the afternoon"
SWIM = "I prefer swimming before school"
RUNNING = "I prefer running at night"
BAG = "I always pack my gym bag the night before"
FOREIGN = "I prefer a latte with green tea and swimming before school"  # another agent's


@pytest.fixture
def seeded(stack):
    ids = {}
    for key, message, value, session in (
        ("tea", "msg_tea", TEA, "ses_tea"),
        ("swim", "msg_swim", SWIM, "ses_swim"),
        ("running", "msg_running", RUNNING, "ses_tea"),
    ):
        receipt = stack.say(message, value, session=session)
        ids[key], ids[f"{key}_evidence"] = receipt["memory_id"], receipt["id"]
    ids["bag"] = stack.write(BAG)["id"]
    ids["foreign"] = stack.write(FOREIGN, agent="companion")["id"]
    # The owner withdraws the running message's support: that memory needs review.
    invalidated = stack.http.post(
        f"/runtime/evidence/{ids['running_evidence']}/invalidate?reason=owner+correction",
        headers=stack.admin_headers(),
    )
    assert invalidated.json()["memories_needing_review"] == [ids["running"]]
    for agent, name in (("owner", "Green Tea Swimmers"), ("companion", "Green Tea Club")):
        created = stack.http.post(
            "/entity/create",
            headers=stack.admin_headers(agent),
            json={"entity_type": "organization", "name": name},
        )
        ids[f"entity_{agent}"] = created.json()["entity"]["id"]
    stack.drain()
    assert ids["foreign"] in stack.index.vectors()  # the other agent's memory is really indexed
    return stack, ids


def memory_ids(body) -> list[str]:
    return [item["id"] for item in body.get("memories", body.get("objects", []))]


def test_conversation_scope_returns_only_that_chats_active_memory(seeded):
    stack, ids = seeded
    tea_chat = {"scope": "conversation", "session_id": "ses_tea"}
    body = stack.context("tea swimming running", include_evidence=True, **tea_chat)
    assert memory_ids(body) == [ids["tea"]]
    assert body["memories"][0]["citations"] == [
        {"message_id": "msg_tea", "session_id": "ses_tea", "content_status": "available"}
    ]
    assert body["retrieval"] == {
        "mode": "explicit-scope",
        "semantic": {"status": "not-used"},
        "limit": 12,
    }
    assert body["entities"] == body["episodes"] == body["evidence"] == [] and body["briefing"] == {}
    assert memory_ids(stack.context("tea", compact=True, **tea_chat)) == [ids["tea"]]
    assert memory_ids(stack.library(**tea_chat)) == [ids["tea"]]
    assert stack.library(object_type="evidence", **tea_chat)["objects"] == []

    swim_chat = {"scope": "conversation", "session_id": "ses_swim"}
    assert memory_ids(stack.context("tea", **swim_chat)) == [ids["swim"]]
    assert stack.context("tea", scope="conversation", session_id="ses_unknown")["memories"] == []
    explore = {"object_type": "memory", "object_id": ids["tea"], **swim_chat}
    assert stack.runtime("explore", explore).status_code == 404
    explored = stack.runtime("explore", {**explore, "object_id": ids["swim"]}).json()
    assert explored["object"]["id"] == ids["swim"] and explored["connections"] == {}

    # The same chat id read with another agent's key finds nothing of the owner's.
    other = stack.runtime(
        "context", {"query": "tea", **tea_chat}, key=stack.companion_key, agent="companion"
    )
    assert other.status_code == 200 and other.json()["memories"] == []


def test_selected_scope_returns_only_listed_active_ids_of_this_namespace(seeded):
    stack, ids = seeded
    listed = [ids["tea"], ids["foreign"], ids["running"], "no-such-memory"]
    selected = {"scope": "selected", "memory_ids": listed}
    body = stack.context("swimming before school", include_evidence=True, **selected)
    assert memory_ids(body) == [ids["tea"]]
    assert body["entities"] == body["evidence"] == []
    assert memory_ids(stack.context("swimming", compact=True, **selected)) == [ids["tea"]]
    assert memory_ids(stack.library(**selected)) == [ids["tea"]]
    for identity in (ids["foreign"], ids["bag"], ids["running"]):
        explore = {"object_type": "memory", "object_id": identity, **selected}
        assert stack.runtime("explore", explore).status_code == 404
    assert FOREIGN not in json.dumps(body) and SWIM not in json.dumps(body)


def test_none_scope_returns_nothing(seeded):
    stack, _ = seeded
    body = stack.context("tea swimming", scope="none", include_evidence=True)
    assert body["memories"] == body["entities"] == body["evidence"] == []
    library = stack.library(scope="none")
    assert library["objects"] == [] and library["total"] == 0


def test_a_read_key_cannot_switch_namespace(seeded):
    stack, ids = seeded
    for route, body in (
        ("context", {"query": "tea"}),
        ("library", {}),
        ("explore", {"object_type": "memory", "object_id": ids["foreign"]}),
    ):
        stack.clear_lockouts()  # each refusal below is checked on its own, not as a lockout
        switched = stack.runtime(route, {**body, "agent_id": "companion"})
        assert switched.status_code == 403 and "latte" not in switched.text
        borrowed_header = stack.runtime(route, body, agent="companion")
        assert borrowed_header.status_code == 401 and "latte" not in borrowed_header.text
        other_key = stack.runtime(route, body, key=stack.companion_key, agent="owner")
        assert other_key.status_code == 401
    no_header = stack.http.post(
        "/runtime/context", headers={"X-MemoryGate-Key": READ_KEY}, json={"query": "tea"}
    )
    assert no_header.status_code == 401
    # A read key reads; it is not an admin, correction or ingestion credential.
    read = stack.read_headers()
    for method, path, body in (
        ("GET", "/memory", None),
        ("POST", "/memory/search", {"query": "tea"}),
        ("POST", "/memory/write", {"text": "I prefer chess"}),
        ("GET", "/audit", None),
        ("GET", f"/memory/{ids['tea']}", None),
        ("POST", "/system/backups", None),
    ):
        stack.clear_lockouts()
        assert stack.http.request(method, path, headers=read, json=body).status_code == 401
    assert (
        stack.http.get(f"/runtime/corrections/memories/{ids['tea']}", headers=read).status_code
        == 401
    )
    ingest = {"session_id": "ses_x", "content": "I prefer chess", "created_at": 1770000000.0}
    assert (
        stack.http.put("/runtime/conversation/msg_x", headers=read, json=ingest).status_code == 401
    )
    assert "chess" not in stack.stored_text()


def test_all_scope_stays_inside_the_keys_namespace(seeded):
    stack, ids = seeded
    owner_ids = {ids["tea"], ids["swim"], ids["bag"]}
    # A query the other agent's memory matches best, by meaning and by words.
    query = "coffee with green tea and swimming before school"
    body = stack.context(query, include_evidence=True)
    assert body["retrieval"]["mode"] == "hybrid"
    assert set(memory_ids(body)) == owner_ids
    assert {item["retrieval_path"] for item in body["memories"]} == {"semantic"}
    assert [item["name"] for item in body["entities"]] == ["Green Tea Swimmers"]
    assert "latte" not in json.dumps(body) and "Green Tea Club" not in json.dumps(body)
    compact = stack.context(query, compact=True)["objects"]
    assert {item["id"] for item in compact if item["type"] == "memory"} == owner_ids
    assert {item["id"] for item in compact if item["type"] == "entity"} == {ids["entity_owner"]}

    library = stack.library()
    assert {item["id"] for item in library["objects"]} >= owner_ids | {ids["entity_owner"]}
    assert ids["foreign"] not in json.dumps(library) and ids["entity_companion"] not in json.dumps(
        library
    )
    found = {item["id"] for item in stack.library(search="Green")["objects"]}
    assert found == {ids["tea"], ids["tea_evidence"], ids["entity_owner"]}
    for kind, identity in (("memory", ids["foreign"]), ("entity", ids["entity_companion"])):
        assert (
            stack.runtime("explore", {"object_type": kind, "object_id": identity}).status_code
            == 404
        )

    # The other agent's own key sees its own memory and none of the owner's.
    theirs = stack.runtime(
        "context", {"query": query}, key=stack.companion_key, agent="companion"
    ).json()
    assert memory_ids(theirs) == [ids["foreign"]]
    assert TEA not in json.dumps(theirs) and SWIM not in json.dumps(theirs)
