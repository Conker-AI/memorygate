"""With the vector index down, reads fall back to lexical and say so; writes queue; recovery drains.

The index is Qdrant local mode switched off (every call refused like a closed port), and
the embedding sidecar can be switched off on its own. A degraded answer must say it is
degraded: no result is labelled semantic unless the index produced it.
"""

import time

import pytest
from app.services import processing_worker
from memory_stack import stack as stack

ESPRESSO = "I always have an espresso before training"  # found by meaning ("coffee"), not words
CHECKLIST = "I prefer the release checklist printed on paper"
WARNING = "Semantic retrieval is unavailable"


def take_down(stack, component: str) -> None:
    if component == "vector_index":
        stack.index.down = True
    else:
        stack.embeddings["up"] = False


def ids(items) -> list[str]:
    return [item["id"] for item in items]


@pytest.mark.parametrize("component", ["vector_index", "embeddings"])
def test_reads_fall_back_to_lexical_and_say_so(stack, component):
    espresso, checklist = stack.write(ESPRESSO)["id"], stack.write(CHECKLIST)["id"]
    found = stack.context("coffee")
    assert found["retrieval"]["mode"] == "hybrid" and found["memories"][0]["id"] == espresso
    assert found["memories"][0]["retrieval_path"] == "semantic"

    take_down(stack, component)
    search = stack.search("coffee")
    assert search["retrieval"]["mode"] == "lexical"
    assert search["retrieval"]["semantic"]["status"] == "degraded"
    assert search["retrieval"]["semantic"]["component"] == component
    assert search["results"] == []  # the match only meaning could find is missing, not faked
    literal = stack.search("release checklist")
    assert [(item["id"], item["retrieval_path"]) for item in literal["results"]] == [
        (checklist, "lexical")
    ]

    context = stack.context("coffee")
    assert context["retrieval"]["mode"] == "lexical"
    assert (
        context["retrieval"]["semantic"]["component"] == component
        and context["retrieval"]["semantic"]["reason"]
    )
    assert espresso not in ids(context["memories"])
    assert (
        WARNING in context["usage"]["instruction"]
        and "incomplete" in context["usage"]["instruction"]
    )
    words = stack.context("release checklist")
    assert ids(words["memories"]) == [checklist]
    assert {item["retrieval_path"] for item in words["memories"]} == {"lexical"}
    compact = stack.context("release checklist", compact=True)
    assert compact["retrieval"]["mode"] == "lexical" and ids(compact["objects"]) == [checklist]
    assert WARNING in compact["usage"]["instruction"]

    # Explicit scope never used the index, so it neither breaks nor claims anything.
    selected = stack.context("coffee", scope="selected", memory_ids=[espresso])
    assert ids(selected["memories"]) == [espresso]
    assert selected["retrieval"]["semantic"] == {"status": "not-used"}
    health = stack.health()
    assert health["status"] == "degraded"
    assert health["degraded"] == (["qdrant"] if component == "vector_index" else ["embeddings"])


def test_an_index_that_fails_mid_request_is_reported_not_hidden(stack):
    espresso = stack.write(ESPRESSO)["id"]
    stack.write(CHECKLIST)
    # The health probe still answers, then the search itself is refused.
    stack.index.failing = {"search"}
    search = stack.search("release checklist")
    assert search["retrieval"] == {
        "mode": "lexical",
        "semantic": {
            "status": "degraded",
            "component": "vector_index",
            "reason": "vector index unreachable",
        },
    }
    context = stack.context("coffee")
    assert context["retrieval"]["mode"] == "lexical" and WARNING in context["usage"]["instruction"]
    assert espresso not in ids(context["memories"])
    assert all(item["retrieval_path"] == "lexical" for item in context["memories"])


def write_during_outage(stack) -> dict:
    """Every kind of write while the index is down; each must be stored and queued."""
    before = {
        "edited": stack.remember("edited", "I prefer maths homework at night")["memory_id"],
        "forgotten": stack.remember("forgotten", "I prefer the old blue bike lock")["memory_id"],
    }
    stack.index.down = True
    written = stack.write(ESPRESSO)
    assert written["status"] == "ok"
    assert (
        written["indexing"]["status"] == "degraded"
        and written["novelty_check"]["status"] == "degraded"
    )
    remembered = stack.remember("stated", "I prefer chess club on Fridays")
    assert remembered["indexing"] == "degraded"
    corrected = stack.correct("edit", before["edited"], 1, "I prefer maths homework in the morning")
    assert corrected.json()["indexing"] == "degraded"
    assert stack.forget("forget", before["forgotten"], 1).json()["index_removal"] == "degraded"
    said = stack.say("msg_swim", "I prefer swimming before school")
    assert said["index_pending"] == "upsert"
    return {
        **before,
        "written": written["id"],
        "stated": remembered["memory_id"],
        "said": said["memory_id"],
    }


def test_writes_while_the_index_is_down_are_queued_not_lost(stack):
    written = write_during_outage(stack)
    queued = {row["object_id"]: row for row in stack.outbox() if row["status"] == "pending"}
    assert set(queued) == set(written.values())
    assert queued[written["forgotten"]]["operation"] == "delete"
    assert queued[written["edited"]]["document_text"] == "I prefer maths homework in the morning"
    # Each write tried the index once and kept the failure; the chat message waits for the worker.
    assert all(
        queued[written[key]]["last_error"] for key in ("written", "stated", "edited", "forgotten")
    )
    assert queued[written["said"]]["last_error"] == ""

    # Stored at once and readable by words, while the index knows none of it.
    for query, key in (
        ("espresso training", "written"),
        ("chess club", "stated"),
        ("maths homework morning", "edited"),
        ("swimming school", "said"),
    ):
        context = stack.context(query)
        assert context["retrieval"]["mode"] == "lexical" and written[key] in ids(
            context["memories"]
        )
    assert written["forgotten"] not in ids(stack.context("old blue bike lock")["memories"])
    retrieval = stack.context("anything")["retrieval"]
    assert (retrieval["pending_vector_index"], retrieval["failed_vector_index"]) == (5, 4)
    assert retrieval["pending_conversation_index"] == 1
    assert not set(stack.index.upserted) & {written["written"], written["stated"], written["said"]}


def test_recovery_drains_the_queue_into_the_index(stack):
    written = write_during_outage(stack)
    stack.index.down = False
    # Back, but not yet drained: meaning cannot find the queued memory yet, words still can.
    assert written["written"] not in ids(stack.context("coffee")["memories"])

    assert stack.drain() == 5
    assert {row["status"] for row in stack.outbox()} == {"complete"}
    assert stack.index.holds(written["written"], ESPRESSO)
    assert stack.index.holds(written["stated"], "I prefer chess club on Fridays")
    assert stack.index.holds(written["edited"], "I prefer maths homework in the morning")
    assert stack.index.holds(written["said"], "I prefer swimming before school")
    assert written["forgotten"] not in stack.index.vectors()

    context = stack.context("coffee")
    assert (
        context["retrieval"]["mode"] == "hybrid" and WARNING not in context["usage"]["instruction"]
    )
    assert context["memories"][0]["id"] == written["written"]
    assert context["memories"][0]["retrieval_path"] == "semantic"
    retrieval = context["retrieval"]
    assert (
        retrieval["pending_vector_index"],
        retrieval["failed_vector_index"],
        retrieval["pending_conversation_index"],
    ) == (0, 0, 0)
    assert stack.search("coffee")["retrieval"]["mode"] == "semantic"


def test_the_processing_worker_drains_the_queue_on_its_own(stack, monkeypatch):
    stack.index.down = True
    queued = {
        stack.write(ESPRESSO)["id"],
        stack.say("msg_swim", "I prefer swimming before school")["memory_id"],
    }
    stack.index.down = False
    stack.later()
    monkeypatch.setattr(processing_worker, "PROCESSING_POLL_SECONDS", 0.05)
    processing_worker.start_worker()
    try:
        deadline = time.monotonic() + 60
        while (
            any(row["status"] == "pending" for row in stack.outbox())
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
    finally:
        processing_worker.stop_worker()
    assert {row["status"] for row in stack.outbox()} == {"complete"}
    assert queued <= set(stack.index.vectors())
    assert stack.context("anything")["retrieval"]["pending_conversation_index"] == 0
