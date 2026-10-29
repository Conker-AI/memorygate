"""Supersede: every read returns the new version, the old one survives only as history.

A memory is superseded through the API in three ways: the owner's reviewed correction
(PUT /runtime/corrections/{request}, compare-and-set on the revision), an admin edit
(PATCH /memory/{id} with expected_revision), and resolving a contradiction between two
memories (POST /memory/conflicts/{id}/resolve). Concurrent supersedes must not both win.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from app.routes import memory as memory_routes
from memory_stack import request_id
from memory_stack import stack as stack

OLD = "I always study physics in the library after school"
NEW = "I always study physics at home after dinner"
NEWER = "I always study physics at the kitchen table before bed"


def returned_texts(stack, memory_id: str) -> list[str]:
    """The memory's text as each read path hands it out, including for its old wording."""
    texts = []
    for query in ("physics", "library after school"):
        texts += [
            item["text"] for item in stack.search(query)["results"] if item["id"] == memory_id
        ]
        texts += [
            item["text"] for item in stack.context(query)["memories"] if item["id"] == memory_id
        ]
        compact = stack.context(query, compact=True)["objects"]
        texts += [item["preview"] for item in compact if item["id"] == memory_id]
    texts += [
        item["preview"]
        for item in stack.library(search="physics")["objects"]
        if item["id"] == memory_id
    ]
    explored = stack.runtime(
        "explore",
        {"object_type": "memory", "object_id": memory_id, "operation": "content", "field": "text"},
    )
    texts.append(explored.json()["content"])
    texts.append(stack.get(f"/memory/{memory_id}").json()["text"])
    texts.append(
        stack.http.get(
            f"/runtime/corrections/memories/{memory_id}", headers=stack.CORRECTION
        ).json()["text"]
    )
    return texts


def history(stack, memory_id: str) -> list[tuple[str, str]]:
    rows = stack.get(f"/memory/{memory_id}/revisions").json()["results"]
    return sorted((row["reason"], row["snapshot"]["text"]) for row in rows)


def test_a_correction_is_what_every_read_returns_and_history_keeps_the_old_text(stack):
    memory_id = stack.write(OLD)["id"]
    assert set(returned_texts(stack, memory_id)) == {OLD}

    first = stack.correct("first", memory_id, 1, NEW)
    assert first.status_code == 200, first.text
    assert (
        first.json()["previous_revision"],
        first.json()["revision"],
        first.json()["indexing"],
    ) == (1, 2, "indexed")
    assert set(returned_texts(stack, memory_id)) == {NEW}
    assert stack.index.holds(memory_id, NEW)

    second = stack.correct("second", memory_id, 2, NEWER)
    assert second.status_code == 200 and second.json()["revision"] == 3
    assert set(returned_texts(stack, memory_id)) == {NEWER}

    # Lineage: every earlier text is kept in revision history, and the receipts chain.
    assert history(stack, memory_id) == [
        ("before reviewed runtime text correction", NEW),
        ("before reviewed runtime text correction", OLD),
        ("memory created", OLD),
    ]
    receipts = [
        stack.http.get(f"/runtime/corrections/{request_id(label)}", headers=stack.CORRECTION).json()
        for label in ("first", "second")
    ]
    assert [(r["previous_revision"], r["revision"]) for r in receipts] == [(1, 2), (2, 3)]

    # A supersede based on a revision that is no longer current is refused and changes nothing.
    stale = stack.correct("stale", memory_id, 2, "I always study physics on the bus")
    assert stale.status_code == 409
    assert set(returned_texts(stack, memory_id)) == {NEWER}
    assert stack.index.holds(memory_id, NEWER)
    assert "bus" not in stack.stored_text()


def test_concurrent_corrections_one_wins_and_the_rest_leave_no_trace(stack):
    memory_id = stack.write(OLD)["id"]
    writers = [
        f"I always study physics with my study partner {name}"
        for name in ("Ada", "Boris", "Chen", "Dana")
    ]

    def correct(index):
        return stack.correct(f"race{index}", memory_id, 1, writers[index]).status_code

    with ThreadPoolExecutor(max_workers=len(writers)) as pool:
        statuses = list(pool.map(correct, range(len(writers))))
    assert sorted(statuses) == [200, 409, 409, 409]
    winner = writers[statuses.index(200)]

    assert set(returned_texts(stack, memory_id)) == {winner}
    assert stack.get(f"/memory/{memory_id}").json()["revision"] == 2
    assert history(stack, memory_id) == [
        ("before reviewed runtime text correction", OLD),
        ("memory created", OLD),
    ]
    assert stack.index.holds(memory_id, winner)
    assert [row["document_text"] for row in stack.outbox() if row["object_id"] == memory_id] == [
        winner
    ]
    stored = stack.stored_text()
    for index, text in enumerate(writers):
        receipt = stack.http.get(
            f"/runtime/corrections/{request_id(f'race{index}')}", headers=stack.CORRECTION
        )
        assert receipt.status_code == (200 if text == winner else 404)
        assert (text in stored) == (text == winner)


def test_an_admin_edit_supersedes_by_revision_too(stack):
    memory_id = stack.write(OLD)["id"]
    edited = stack.http.patch(
        f"/memory/{memory_id}",
        headers=stack.admin_headers(),
        json={"expected_revision": 1, "text": NEW},
    )
    assert edited.status_code == 200, edited.text
    assert set(returned_texts(stack, memory_id)) == {NEW}
    assert ("before manual edit", OLD) in history(stack, memory_id)

    stale = stack.http.patch(
        f"/memory/{memory_id}",
        headers=stack.admin_headers(),
        json={"expected_revision": 1, "text": NEWER},
    )
    assert stale.status_code == 409
    assert set(returned_texts(stack, memory_id)) == {NEW}


COFFEE = "I always drink coffee before morning training"
TEA = "I prefer tea now and stopped drinking coffee before morning training"


def active_memory_ids(stack) -> set[str]:
    found = {item["id"] for item in stack.context("coffee before morning training")["memories"]}
    return found | {item["id"] for item in stack.library(object_type="memory")["objects"]}


def contradiction(stack) -> tuple[str, str, str]:
    old, new = stack.write(COFFEE)["id"], stack.write(TEA)["id"]
    [conflict] = stack.get("/memory/conflicts").json()["results"]
    assert conflict["status"] == "open"
    return old, new, conflict["id"]


def test_resolving_a_contradiction_supersedes_the_loser(stack):
    old, new, conflict_id = contradiction(stack)
    # Until the owner decides, neither side of a contradiction reaches the reading model.
    assert active_memory_ids(stack) & {old, new} == set()

    resolved = stack.http.post(
        f"/memory/conflicts/{conflict_id}/resolve",
        headers=stack.admin_headers(),
        json={"winner_memory_id": new},
    )
    assert resolved.status_code == 200, resolved.text
    assert active_memory_ids(stack) & {old, new} == {new}
    assert stack.runtime("explore", {"object_type": "memory", "object_id": old}).status_code == 404

    # The loser is kept, marked, and its history says why.
    loser = stack.get(f"/memory/{old}").json()
    assert loser["status"] == "needs_review" and loser["valid_until"] is not None
    assert ("not selected during conflict resolution", COFFEE) in history(stack, old)
    assert [row["status"] for row in stack.get("/memory/conflicts").json()["results"]] == [
        "resolved"
    ]


def test_concurrent_resolutions_do_not_both_win(stack, monkeypatch):
    old, new, conflict_id = contradiction(stack)
    # Both requests read the conflict before either writes: the race a double click makes.
    barrier, waited = threading.Barrier(2, timeout=30), set()
    original = memory_routes.add_revision

    def after_both_read(db, memory, reason, changed_by="system"):
        if id(db) not in waited:
            waited.add(id(db))
            barrier.wait()
        return original(db, memory, reason, changed_by)

    monkeypatch.setattr(memory_routes, "add_revision", after_both_read)

    def resolve(winner):
        return stack.http.post(
            f"/memory/conflicts/{conflict_id}/resolve",
            headers=stack.admin_headers(),
            json={"winner_memory_id": winner},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(resolve, (old, new)))
    assert sorted(response.status_code for response in responses) == [200, 409], [
        r.text for r in responses
    ]
    winner = (old, new)[[response.status_code for response in responses].index(200)]
    statuses = {
        identity: stack.get(f"/memory/{identity}").json()["status"] for identity in (old, new)
    }
    assert statuses == {winner: "active", ({old, new} - {winner}).pop(): "needs_review"}
    assert active_memory_ids(stack) & {old, new} == {winner}
    assert (
        json.loads(responses[[r.status_code for r in responses].index(409)].text)["detail"]["code"]
        == "revision_conflict"
    )
