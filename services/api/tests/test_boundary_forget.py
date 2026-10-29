"""A forgotten memory never comes back, through any read path, even after the index catches up.

Everything goes through the real HTTP API (see memory_stack.py). "Every read path" means
what the dashboard, the owner's admin tools and Pi can read: list, get, search, revisions,
conflicts, lineage, audit, briefing, runtime context (full, compact, selected), library,
explorer, the correction capability's own read, and a backup exported through the API.
"""

import json

import pytest
from memory_stack import OWNER, request_id
from memory_stack import stack as stack

SECRET = "My spare house key hides under the blue flowerpot by the gate"
MOVED = "My spare house key now hides under the red flowerpot by the back door"
MARK = "flowerpot"  # in both versions and in nothing else
KEEP = "I always train before school on Mondays"
QUERIES = ("flowerpot", "spare house key", SECRET, MOVED)


def retrieved_ids(stack) -> set[str]:
    """Ids every retrieval path returns for queries that match the secret."""
    ids = set()
    for query in QUERIES:
        ids |= {item["id"] for item in stack.search(query)["results"]}
        ids |= {item["id"] for item in stack.context(query)["memories"]}
        ids |= {item["id"] for item in stack.context(query, compact=True)["objects"]}
    for body in ({}, {"search": MARK}, {"object_type": "memory"}):
        ids |= {item["id"] for item in stack.library(**body)["objects"]}
    return ids


def everything_readable(stack, memory_id: str) -> str:
    paths = (
        "/memory",
        f"/memory/{memory_id}",
        f"/memory/{memory_id}/revisions",
        "/memory/conflicts",
        f"/lineage/memory/{memory_id}",
        "/audit",
        f"/briefing/{OWNER}",
    )
    parts = [stack.get(path).text for path in paths]
    for query in QUERIES:
        # Context echoes the query; only what it returns counts.
        full, compact = (
            stack.context(query),
            stack.context(query, compact=True, include_evidence=True),
        )
        parts += [
            json.dumps(stack.search(query)),
            json.dumps({**full, "query": ""}),
            json.dumps({**compact, "query": ""}),
        ]
    selected = stack.context("key", scope="selected", memory_ids=[memory_id])
    parts.append(json.dumps(selected))
    parts += [json.dumps(stack.library()), json.dumps(stack.library(search=MARK))]
    for operation in ("connections", "content"):
        parts.append(
            stack.runtime(
                "explore",
                {
                    "object_type": "memory",
                    "object_id": memory_id,
                    "operation": operation,
                    "field": "text",
                },
            ).text
        )
    parts.append(
        stack.http.get(f"/runtime/corrections/memories/{memory_id}", headers=stack.CORRECTION).text
    )
    parts.append(stack.backup())
    return "\n".join(parts)


def remembered_and_corrected(stack) -> str:
    """An owner-approved memory, then corrected once, so revision history holds both texts."""
    memory_id = stack.remember("secret", SECRET)["memory_id"]
    assert stack.correct("move", memory_id, 1, MOVED).json()["revision"] == 2
    stack.write(KEEP)
    return memory_id


def forget_by(stack, how: str, memory_id: str, revision: int):
    if how == "owner_forget":
        response = stack.forget("forget", memory_id, revision)
        assert response.json()["index_removal"] == "removed", response.text
    else:
        response = stack.http.delete(
            f"/memory/{memory_id}?expected_revision={revision}", headers=stack.admin_headers()
        )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("how", ["owner_forget", "admin_delete"])
def test_a_forgotten_memory_is_gone_from_every_read_path(stack, how):
    memory_id = remembered_and_corrected(stack)
    # Control: before forgetting, these same reads do return it.
    assert memory_id in retrieved_ids(stack)
    assert MARK in everything_readable(stack, memory_id)
    assert stack.index.holds(memory_id, MOVED)

    forget_by(stack, how, memory_id, 2)

    assert memory_id not in retrieved_ids(stack)
    assert MARK not in everything_readable(stack, memory_id)
    for path in (
        f"/memory/{memory_id}",
        f"/memory/{memory_id}/revisions",
        f"/lineage/memory/{memory_id}",
    ):
        assert stack.get(path).status_code == 404
    explore = stack.runtime("explore", {"object_type": "memory", "object_id": memory_id})
    assert explore.status_code == 404
    selected = stack.context(MARK, scope="selected", memory_ids=[memory_id])
    assert selected["memories"] == []
    assert memory_id not in stack.index.vectors()
    assert MARK not in stack.stored_text()
    # The unrelated memory is untouched.
    assert KEEP in json.dumps(stack.context("train before school"))


def test_replaying_the_requests_that_made_it_does_not_bring_it_back(stack):
    memory_id = remembered_and_corrected(stack)
    forget_by(stack, "owner_forget", memory_id, 2)

    # Pi's outbox retries the old requests after the forget: receipts answer, nothing is stored.
    replayed = stack.http.put(
        f"/runtime/corrections/remember/{request_id('secret')}",
        headers=stack.CORRECTION,
        json={"text": SECRET},
    )
    assert replayed.status_code == 200 and replayed.json()["memory_id"] == memory_id
    assert stack.correct("move", memory_id, 1, MOVED).status_code == 200
    assert stack.forget("forget", memory_id, 2).json()["status"] == "forgotten"

    assert stack.get(f"/memory/{memory_id}").status_code == 404
    assert memory_id not in retrieved_ids(stack)
    assert MARK not in stack.stored_text()


def test_forgetting_while_the_index_is_down_holds_after_it_catches_up(stack):
    memory_id = stack.remember("secret", SECRET)["memory_id"]
    assert stack.index.holds(memory_id, SECRET)

    stack.index.down = True
    forgotten = stack.forget("forget", memory_id, 1)
    assert forgotten.status_code == 200 and forgotten.json()["index_removal"] == "degraded"
    [intent] = [row for row in stack.outbox() if row["object_id"] == memory_id]
    assert (intent["operation"], intent["status"], intent["document_text"]) == (
        "delete",
        "pending",
        None,
    )
    assert memory_id not in retrieved_ids(stack)

    # The index is back but still holds the stale point: semantic hits for it are not trusted.
    stack.index.down = False
    assert memory_id in stack.index.vectors()
    assert stack.context("spare house key")["retrieval"]["mode"] == "hybrid"
    assert memory_id not in retrieved_ids(stack)

    stack.drain()
    assert memory_id not in stack.index.vectors()
    assert [row["status"] for row in stack.outbox() if row["object_id"] == memory_id] == [
        "complete"
    ]
    assert memory_id not in retrieved_ids(stack)


def test_a_queued_upsert_never_resurrects_a_forgotten_memory(stack):
    stack.index.down = True
    memory_id = stack.remember("secret", SECRET)["memory_id"]
    assert stack.correct("move", memory_id, 1, MOVED).json()["indexing"] == "degraded"
    assert stack.forget("forget", memory_id, 2).json()["index_removal"] == "degraded"
    # The newest intent replaced the queued upserts and carries no text.
    [intent] = [row for row in stack.outbox() if row["object_id"] == memory_id]
    assert (intent["operation"], intent["document_text"]) == ("delete", None)

    stack.index.down = False
    stack.drain()
    assert memory_id not in stack.index.upserted
    assert memory_id not in stack.index.vectors()
    assert memory_id not in retrieved_ids(stack)
    assert MARK not in stack.stored_text() and MARK not in stack.backup()


def test_a_memory_from_chat_stays_forgotten_and_its_message_can_be_forgotten_too(stack):
    said = "Remember that I park my bike behind the gym on weekdays"
    receipt = stack.say("msg_bike", said)
    assert receipt["state"] == "admitted"
    memory_id = receipt["memory_id"]
    stack.drain()
    in_chat = {"scope": "conversation", "session_id": "ses_one"}
    assert [item["id"] for item in stack.context("bike", **in_chat)["memories"]] == [memory_id]

    assert stack.forget("forget", memory_id, 1).status_code == 200
    assert stack.context("bike", **in_chat)["memories"] == []
    assert memory_id not in {item["id"] for item in stack.context("bike gym")["memories"]}
    assert "gym" not in json.dumps(stack.library(object_type="memory"))
    assert (
        stack.runtime("explore", {"object_type": "memory", "object_id": memory_id}).status_code
        == 404
    )

    # Pi redelivering the same message does not admit it again.
    assert stack.say("msg_bike", said)["memory_id"] == memory_id
    assert stack.get(f"/memory/{memory_id}").status_code == 404
    assert stack.context("bike", **in_chat)["memories"] == []

    # By design the chat message itself is source evidence, not the memory: it stays
    # until the owner forgets the message, and then its text is gone everywhere.
    assert "gym" in json.dumps(stack.library(object_type="evidence"))
    assert stack.unsay("msg_bike")["state"] == "deleted"
    assert "gym" not in json.dumps(stack.library())
    assert "gym" not in stack.stored_text() and "gym" not in stack.backup()
