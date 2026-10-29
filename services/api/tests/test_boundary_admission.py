"""Admission: what the owner asked to remember is stored with its source; filtered content is not.

Content arrives three ways: Pi delivers the owner's chat messages (admitted when the owner
asks to remember or the message carries a signal, filtered otherwise; "don't remember" is
always filtered), the owner approves a statement through the correction capability (stored
word for word), and the admin write route (filtered below the agent's value threshold).
"""

import json

import pytest
from memory_stack import request_id
from memory_stack import stack as stack

ASKED = "Remember that my physics exam moved to Thursday"


def memory_reads(stack, query: str) -> str:
    """Every memory view, without the query echo, as one string."""
    parts = [
        stack.get("/memory").text,
        json.dumps(stack.search(query)),
        json.dumps(stack.library(object_type="memory")),
    ]
    for body in ({}, {"compact": True}, {"scope": "conversation", "session_id": "ses_school"}):
        parts.append(json.dumps({**stack.context(query, **body), "query": ""}))
    return "\n".join(parts)


def test_a_remember_request_in_chat_is_stored_word_for_word_with_its_source(stack):
    receipt = stack.say("msg_exam", ASKED, session="ses_school")
    assert (receipt["state"], receipt["admission_method"]) == ("admitted", "owner_requested")
    memory_id = receipt["memory_id"]

    stored = stack.get(f"/memory/{memory_id}").json()
    assert (
        stored["text"],
        stored["source_type"],
        stored["confidence"],
        stored["do_not_generalize"],
    ) == (ASKED, "owner_statement", "medium", True)
    [item] = [item for item in stack.context("physics exam")["memories"] if item["id"] == memory_id]
    assert (item["text"], item["source_type"]) == (ASKED, "owner_statement")
    assert item["citations"] == [
        {"message_id": "msg_exam", "session_id": "ses_school", "content_status": "available"}
    ]
    in_chat = stack.context("exam", scope="conversation", session_id="ses_school")
    assert [entry["id"] for entry in in_chat["memories"]] == [memory_id]

    # The source is one hop away for the reader: memory <- admission analysis <- the message.
    explored = stack.runtime("explore", {"object_type": "memory", "object_id": memory_id}).json()
    assert explored["connections"] == {"supports": 1}
    [analysis] = [node for node in explored["nodes"] if node["type"] == "analysis"]
    steps = stack.runtime(
        "explore",
        {
            "object_type": "analysis",
            "object_id": analysis["id"],
            "operation": "content",
            "field": "steps_json",
        },
    ).json()["content"]
    assert json.loads(steps) == ["owner_requested_retention", "retain_owner_attribution"]
    source = stack.runtime(
        "explore",
        {
            "object_type": "evidence",
            "object_id": receipt["id"],
            "operation": "content",
            "field": "normalized_payload_json",
        },
    ).json()
    assert json.loads(source["content"]) == {"content": ASKED}


def test_an_owner_approved_statement_is_stored_exactly_with_a_receipt(stack):
    fact = "The physics exam is on Thursday 30 October."
    receipt = stack.remember("fact", fact)
    assert (receipt["status"], receipt["indexing"]) == ("remembered", "indexed")
    stored = stack.get(f"/memory/{receipt['memory_id']}").json()
    assert (stored["text"], stored["summary"], stored["source_type"], stored["confidence"]) == (
        fact,
        fact,
        "user",
        "high",
    )
    [item] = [
        item
        for item in stack.context("physics exam")["memories"]
        if item["id"] == receipt["memory_id"]
    ]
    assert (item["text"], item["source_type"]) == (fact, "user")
    assert stack.index.holds(receipt["memory_id"], fact)
    looked_up = stack.http.get(
        f"/runtime/corrections/{request_id('fact')}", headers=stack.CORRECTION
    ).json()
    assert looked_up["memory_id"] == receipt["memory_id"] and "text" not in looked_up
    # The audit says who stored it and why, without a second copy of the words.
    [audit] = [
        row for row in stack.get("/audit").json() if row["memory_id"] == receipt["memory_id"]
    ]
    assert audit["action"] == "owner_stated_memory" and "physics" not in audit["payload_json"]


def test_a_signal_in_ordinary_chat_is_admitted_but_not_as_an_owner_request(stack):
    receipt = stack.say("msg_tea", "I prefer green tea in the afternoon", session="ses_school")
    assert (receipt["state"], receipt["admission_method"]) == ("admitted", "signal")
    stored = stack.get(f"/memory/{receipt['memory_id']}").json()
    assert stored["source_type"] == "owner_statement" and stored["do_not_generalize"] is True


@pytest.mark.parametrize(
    "said,mark",
    [
        ("ok thanks", "thanks"),  # no signal
        ("Don't remember that I prefer pineapple on pizza", "pineapple"),  # a signal, declined
        ("Please don't remember this: I hate my piano teacher", "piano"),
    ],
)
def test_filtered_chat_never_becomes_memory(stack, said, mark):
    receipt = stack.say("msg_filtered", said, session="ses_school")
    assert (receipt["state"], receipt["admission_method"], receipt["memory_id"]) == (
        "filtered",
        "filtered",
        None,
    )
    assert mark not in memory_reads(stack, mark)

    # Neither Pi redelivering the message nor the background worker promotes it later.
    assert stack.say("msg_filtered", said, session="ses_school")["state"] == "filtered"
    stack.drain()
    assert mark not in memory_reads(stack, mark)
    assert stack.outbox() == []
    # By design filtering is not deletion: the message stays as raw source evidence
    # (an explicit evidence read), until Pi forgets the message.
    evidence = stack.context(mark, include_evidence=True)["evidence"]
    assert [row["id"] for row in evidence] == [receipt["id"]]


def test_an_admin_write_below_the_value_threshold_is_filtered(stack):
    response = stack.http.post(
        "/memory/write",
        headers=stack.admin_headers(),
        json={"text": "The weather was grey on the walk home", "source_type": "user"},
    )
    assert response.status_code == 200 and response.json() == {
        "status": "filtered",
        "reason": "low value",
    }
    assert "grey" not in memory_reads(stack, "weather grey walk")
    assert stack.outbox() == []
    writes = [
        row["action"] for row in stack.get("/audit").json() if row["action"] != "runtime_context"
    ]
    assert writes == ["filtered"]

    kept = stack.write("I always walk home the long way when it is grey", source_type="user")
    stored = stack.get(f"/memory/{kept['id']}").json()
    assert (stored["status"], stored["source_type"]) == ("active", "user")
    assert kept["id"] in [item["id"] for item in stack.context("walk home grey")["memories"]]
