"""A refused write leaves no copy of its words; a memory the index has not seen yet stays findable.

A `/memory/write` below the value threshold creates no memory, so nothing could forget its
text later: the audit keeps only the refusal. And `/memory/search` adds word matches for
memories still queued for the vector index, so a memory written while the index was down
is found by its words even after the index answers again. Every call goes through the real
HTTP API; the index is Qdrant local mode behind the harness's switch.
"""

import json

from memory_stack import stack as stack

GREY = "The weather was grey on the walk home"  # below the default value threshold
CHECKLIST = "I prefer the release checklist printed on paper"
ESPRESSO = "I always have an espresso before training"


def by_id(body) -> dict[str, str]:
    return {item["id"]: item["retrieval_path"] for item in body["results"]}


def test_a_refused_write_keeps_only_the_refusal(stack):
    response = stack.http.post("/memory/write", headers=stack.admin_headers(), json={"text": GREY})
    assert response.json() == {"status": "filtered", "reason": "low value"}

    audit = stack.get("/audit")
    assert audit.status_code == 200 and "grey" not in audit.text.lower()
    [refusal] = audit.json()
    assert (refusal["action"], refusal["memory_id"]) == ("filtered", None)
    payload = json.loads(refusal["payload_json"])
    assert payload["reason"] == "low_value" and payload["text_length"] == len(GREY)
    assert set(payload) == {"agent_id", "reason", "text_length", "value_score"}
    assert "grey" not in stack.stored_text().lower()  # nor anywhere else


def test_search_finds_a_memory_still_queued_for_the_index_by_its_words(stack):
    stack.index.down = True
    espresso = stack.write(ESPRESSO)["id"]
    foreign = stack.write(ESPRESSO + " on Sundays", agent="companion")["id"]
    stack.index.down = False  # back, but the queue has not drained

    # The index holds nothing yet, and the words are not one literal phrase of the memory.
    alone = stack.search("espresso training")
    assert by_id(alone) == {espresso: "lexical"}
    assert (alone["retrieval"]["mode"], alone["retrieval"]["pending_vector_index"]) == (
        "lexical",
        1,
    )

    checklist = stack.write(CHECKLIST)["id"]
    # The index answers with the only memory it holds; the queued one is added by its words.
    found = stack.search("espresso training")
    assert by_id(found) == {checklist: "semantic", espresso: "lexical"}
    assert foreign not in by_id(found)  # another agent's queued memory stays out
    assert found["retrieval"]["mode"] == "hybrid"
    assert found["retrieval"]["semantic"]["status"] == "ok"
    assert found["retrieval"]["pending_vector_index"] == 1
    [queued] = [item for item in found["results"] if item["id"] == espresso]
    assert queued["text"] == ESPRESSO and "similarity" not in queued
    # Words that match nothing queued add nothing.
    assert by_id(stack.search("release checklist")) == {checklist: "semantic"}

    stack.drain()
    drained = stack.search("espresso training")
    assert by_id(drained) == {espresso: "semantic", checklist: "semantic"}
    assert (drained["retrieval"]["mode"], drained["retrieval"]["pending_vector_index"]) == (
        "semantic",
        0,
    )


def test_a_memory_forgotten_while_queued_is_not_found_by_its_words(stack):
    stack.write(CHECKLIST)
    stated = stack.remember("espresso", ESPRESSO)
    stack.index.down = True
    assert stack.forget("forget", stated["memory_id"], 1).json()["index_removal"] == "degraded"
    stack.index.down = False

    found = stack.search("espresso training")
    assert stated["memory_id"] not in by_id(found)
    assert found["retrieval"]["pending_vector_index"] == 1  # the queued removal
    assert "espresso" not in json.dumps(found["results"])
