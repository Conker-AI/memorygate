"""Who wrote a stated memory stays visible: the owner's words, or words Conker wrote itself.

Pi stores both through the correction capability's statement route. Owner text (typed, or
approved word for word) is a high-confidence user memory; Conker's own text is a
medium-confidence assistant memory with its own history note and audit action. Every call
goes through the real HTTP API.
"""

import json

import pytest
from app.services.qdrant_store import QDRANT_COLLECTION
from memory_stack import request_id
from memory_stack import stack as stack

OWNER_FACT = "The physics exam is on Thursday 30 October."
CONKER_FACT = "The owner seems to study physics best in the evening."
DEFAULT = object()  # leave `origin` out of the request


def state(stack, label: str, value: str, origin=DEFAULT):
    body = {"text": value} if origin is DEFAULT else {"text": value, "origin": origin}
    return stack.http.put(
        f"/runtime/corrections/remember/{request_id(label)}", headers=stack.CORRECTION, json=body
    )


def stated(stack, label: str, value: str, origin=DEFAULT) -> dict:
    response = state(stack, label, value, origin)
    assert response.status_code == 200, response.text
    return response.json()


def audits(stack) -> list[dict]:
    return [row for row in stack.get("/audit").json() if row["memory_id"] is not None]


def test_conker_text_is_kept_as_an_assistant_memory_a_reader_can_tell_apart(stack):
    owner = stated(stack, "owner", OWNER_FACT)
    conker = stated(stack, "conker", CONKER_FACT, origin="conker")
    assert (conker["status"], conker["indexing"]) == ("remembered", "indexed")

    stored = stack.get(f"/memory/{conker['memory_id']}").json()
    assert (stored["text"], stored["summary"], stored["source_type"], stored["confidence"]) == (
        CONKER_FACT,
        CONKER_FACT,
        "assistant",
        "medium",
    )
    assert stack.get(f"/memory/{owner['memory_id']}").json()["source_type"] == "user"
    [revision] = stack.get(f"/memory/{conker['memory_id']}/revisions").json()["results"]
    assert (revision["changed_by"], revision["reason"]) == ("conker", "Conker-written statement")

    # Each origin has its own audit action; neither keeps a second copy of the words.
    by_memory = {row["memory_id"]: row for row in audits(stack)}
    assert by_memory[owner["memory_id"]]["action"] == "owner_stated_memory"
    assert by_memory[conker["memory_id"]]["action"] == "conker_stated_memory"
    assert json.loads(by_memory[conker["memory_id"]]["payload_json"]) == {
        "request_id": request_id("conker")
    }

    # Every recall view carries the source type, so the reader knows who said it.
    expected = {owner["memory_id"]: "user", conker["memory_id"]: "assistant"}
    for body in ({}, {"scope": "selected", "memory_ids": list(expected)}):
        found = stack.context("physics", **body)["memories"]
        assert {item["id"]: item["source_type"] for item in found} == expected
    compact = stack.context("physics", compact=True)["objects"]
    assert {item["id"]: item["source_type"] for item in compact} == expected
    library = stack.library(object_type="memory")["objects"]
    assert {item["id"]: item["source_type"] for item in library} == expected
    searched = stack.search("physics")["results"]
    assert {item["id"]: item["source_type"] for item in searched} == expected

    # The index entry says the same, read straight from the index.
    [point] = stack.index.client.retrieve(
        QDRANT_COLLECTION, ids=[conker["memory_id"]], with_payload=True
    )
    assert (point.payload["source_type"], point.payload["confidence"]) == ("assistant", "medium")


def test_owner_is_the_default_and_hashes_as_before(stack):
    first = stated(stack, "fact", OWNER_FACT)
    # Naming the default origin is the same request, so it replays instead of conflicting.
    assert stated(stack, "fact", OWNER_FACT, origin="owner") == first
    stored = stack.get(f"/memory/{first['memory_id']}").json()
    assert (stored["source_type"], stored["confidence"]) == ("user", "high")
    assert [row["action"] for row in audits(stack)] == ["owner_stated_memory"]


def test_a_conker_request_replays_once_and_cannot_change_origin(stack):
    first = stated(stack, "keep", CONKER_FACT, origin="conker")
    assert stated(stack, "keep", CONKER_FACT, origin="conker") == first
    # The same request id cannot be re-sent as the owner's words, nor the other way round.
    assert state(stack, "keep", CONKER_FACT).status_code == 409
    stated(stack, "said", OWNER_FACT)
    assert state(stack, "said", OWNER_FACT, origin="conker").status_code == 409

    memories = stack.get("/memory").json()
    assert sorted(item["source_type"] for item in memories) == ["assistant", "user"]
    assert sorted(row["action"] for row in audits(stack)) == [
        "conker_stated_memory",
        "owner_stated_memory",
    ]
    assert [row["object_id"] for row in stack.outbox()].count(first["memory_id"]) == 1


@pytest.mark.parametrize("origin", ["Owner", "pi", "assistant", "", 1, None, True])
def test_an_unknown_origin_stores_nothing(stack, origin):
    response = state(stack, "bad", CONKER_FACT, origin=origin)
    assert response.status_code == 422 and CONKER_FACT not in response.text
    assert stack.get("/memory").json() == []
    assert CONKER_FACT not in stack.stored_text()
