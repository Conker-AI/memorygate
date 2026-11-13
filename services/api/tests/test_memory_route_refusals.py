"""Memory and system routes: refused calls change nothing, and another agent's rows stay hidden.

Every call goes through the real HTTP API with the admin key. A memory or conflict belongs to one
agent: any other agent asking for it by id gets 404 and the row stays exactly as its owner left
it. A stale revision, an unknown memory type, a winner outside the conflict or bad paging is
refused and nothing is stored: the memory, its revision history and the conflict read back the
same. A write that repeats a memory in other words upgrades the first one instead of adding a
second; a write whose near-duplicate check cannot run still stores and says the check did not
run. On the system routes a missing or foreign backup file is never served, and a refused model
change leaves the stored setting as it was.
"""

from pathlib import Path

from app.models.ai_runtime_setting import AiRuntimeSetting
from app.services import backup_service, pi_inference
from app.services.qdrant_store import INDEX_UNREACHABLE
from memory_stack import ADMIN_KEY
from memory_stack import stack as stack

OTHER = "companion"
COFFEE = "I always drink coffee before morning training"
TEA = "I prefer tea now and stopped drinking coffee before morning training"


def memory(stack, memory_id, agent="owner"):
    return stack.get(f"/memory/{memory_id}", agent)


def revisions(stack, memory_id, agent="owner"):
    response = stack.get(f"/memory/{memory_id}/revisions", agent)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def patch(stack, memory_id, body, agent="owner"):
    return stack.http.patch(f"/memory/{memory_id}", headers=stack.admin_headers(agent), json=body)


def delete(stack, memory_id, agent="owner", **params):
    return stack.http.delete(
        f"/memory/{memory_id}", headers=stack.admin_headers(agent), params=params
    )


def resolve(stack, conflict_id, winner, agent="owner"):
    return stack.http.post(
        f"/memory/conflicts/{conflict_id}/resolve",
        headers=stack.admin_headers(agent),
        json={"winner_memory_id": winner},
    )


def conflicts(stack, agent="owner"):
    return stack.get("/memory/conflicts", agent).json()["results"]


def contradiction(stack, agent="owner"):
    old, new = stack.write(COFFEE, agent=agent)["id"], stack.write(TEA, agent=agent)["id"]
    [conflict] = conflicts(stack, agent)
    assert conflict["status"] == "open"
    return old, new, conflict["id"]


def snapshot(stack, memory_id, agent="owner"):
    """What a refusal must leave untouched: the memory as read and its whole history."""
    return memory(stack, memory_id, agent).json(), revisions(stack, memory_id, agent)


# --- reading ----------------------------------------------------------------


def test_another_agents_memory_is_not_found_by_id_or_in_its_list(stack):
    mine = stack.write("I prefer window seats on long flights")["id"]

    for path in (f"/memory/{mine}", f"/memory/{mine}/revisions"):
        response = stack.get(path, OTHER)
        assert response.status_code == 404
        assert response.json()["detail"] == "Memory not found"
    assert stack.get("/memory", OTHER).json() == []
    assert memory(stack, mine).status_code == 200


def test_an_unknown_memory_id_is_not_found(stack):
    for path in ("/memory/mem_missing", "/memory/mem_missing/revisions"):
        response = stack.get(path)
        assert response.status_code == 404
        assert response.json()["detail"] == "Memory not found"


def test_bad_paging_is_refused_and_a_page_past_the_end_is_empty(stack):
    mine = stack.write("I prefer green tea in the afternoon")["id"]
    for params in ({"limit": 501}, {"offset": -1}, {"limit": "ten"}):
        response = stack.http.get("/memory", params=params, headers=stack.admin_headers())
        assert response.status_code == 422, params
        assert response.json()["detail"][0]["loc"][0] == "query"

    past = stack.http.get("/memory", params={"offset": 1}, headers=stack.admin_headers())
    assert past.status_code == 200 and past.json() == []
    assert [row["id"] for row in stack.get("/memory").json()] == [mine]


# --- writing ----------------------------------------------------------------


def test_a_write_with_an_unknown_memory_type_is_refused_and_stores_nothing(stack):
    text = "I prefer aisle seats when the flight is short"
    response = stack.http.post(
        "/memory/write",
        headers=stack.admin_headers(),
        json={"text": text, "memory_type": "opinion"},
    )
    assert response.status_code == 422
    assert response.json()["detail"].startswith("Unrecognized memory_type 'opinion'")
    assert stack.get("/memory").json() == []
    assert text not in stack.stored_text()
    assert stack.outbox() == []


def test_a_repeat_in_other_words_upgrades_the_first_memory_instead_of_adding_one(stack):
    first = stack.write("I prefer coffee every morning", tags=["drinks"])
    assert first["status"] == "ok"

    # The test embedder reads "espresso" as "coffee": the second text is the same meaning.
    again = stack.write("I prefer espresso every morning", tags=["mornings"])

    assert again["near_duplicate_of"] == first["id"] == again["id"]
    assert again["upgraded"] is True
    assert again["similarity"] > 0.99
    [row] = stack.get("/memory").json()
    assert row["id"] == first["id"]
    assert row["text"] == "I prefer coffee every morning"
    assert row["tags"] == ["drinks", "mornings"]
    reasons = sorted(item["reason"] for item in revisions(stack, first["id"]))
    assert reasons == ["duplicate upgraded existing memory", "memory created"]


def test_an_exact_repeat_with_nothing_new_changes_nothing(stack):
    first = stack.write("I prefer tea after dinner", tags=["drinks"])
    before = snapshot(stack, first["id"])

    again = stack.write("  i prefer TEA after dinner ", tags=["drinks"])

    assert (again["duplicate_of"], again["upgraded"]) == (first["id"], False)
    assert snapshot(stack, first["id"]) == before
    assert len(stack.get("/memory").json()) == 1


def test_a_write_whose_near_duplicate_check_cannot_run_still_stores_and_says_so(stack):
    first = stack.write("I prefer coffee every morning")["id"]
    stack.index.failing.add("search")  # the index answers writes but not similarity queries

    # With the check running this text would upgrade the first memory (see the test above);
    # without it the write must still be stored, as a memory of its own, and say so.
    written = stack.write("I prefer espresso every morning")

    assert written["novelty_check"] == {
        "status": "degraded",
        "component": "vector_index",
        "reason": INDEX_UNREACHABLE,
    }
    assert "indexing" not in written
    assert written["id"] != first
    assert "near_duplicate_of" not in written
    texts = sorted(row["text"] for row in stack.get("/memory").json())
    assert texts == ["I prefer coffee every morning", "I prefer espresso every morning"]
    assert memory(stack, written["id"]).json()["text"] == "I prefer espresso every morning"
    assert stack.index.holds(written["id"], "I prefer espresso every morning")


# --- editing ----------------------------------------------------------------


def test_another_agent_cannot_edit_a_memory_and_it_stays_unchanged(stack):
    mine = stack.write("I prefer quiet carriages on trains")["id"]
    before = snapshot(stack, mine)

    response = patch(stack, mine, {"text": "changed by someone else"}, OTHER)

    assert response.status_code == 404
    assert response.json()["detail"] == "Memory not found"
    assert snapshot(stack, mine) == before
    assert "changed by someone else" not in stack.stored_text()


def test_an_edit_of_an_unknown_memory_is_not_found(stack):
    response = patch(stack, "mem_missing", {"text": "anything at all"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Memory not found"
    assert "anything at all" not in stack.stored_text()


def test_an_edit_with_a_stale_revision_is_refused_and_changes_nothing(stack):
    mine = stack.write("I prefer early meetings on Mondays")["id"]
    before = snapshot(stack, mine)
    current = before[0]["revision"]

    response = patch(stack, mine, {"expected_revision": current + 1, "text": "late meetings"})

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "revision_conflict", "current_revision": current}
    assert snapshot(stack, mine) == before


def test_an_edit_with_a_malformed_revision_is_refused_and_changes_nothing(stack):
    mine = stack.write("I prefer paper notebooks for planning")["id"]
    before = snapshot(stack, mine)

    for revision in (0, -1, "1", 1.5, True):
        response = patch(stack, mine, {"expected_revision": revision, "text": "digital notes"})
        assert response.status_code == 422, revision
        assert response.json()["detail"][0]["loc"] == ["body", "expected_revision"]
    assert snapshot(stack, mine) == before


def test_an_edit_to_an_unknown_memory_type_is_refused_and_keeps_the_text_too(stack):
    mine = stack.write("I prefer running in the evening")["id"]
    before = snapshot(stack, mine)

    response = patch(stack, mine, {"text": "I prefer running at dawn", "memory_type": "opinion"})

    assert response.status_code == 422
    assert response.json()["detail"].startswith("Unrecognized memory_type 'opinion'")
    # The text in the same request was applied in memory before the type check; none of it,
    # nor the "before manual edit" revision, may reach the store.
    assert snapshot(stack, mine) == before
    assert "I prefer running at dawn" not in stack.stored_text()


def test_an_edit_of_confidence_flags_and_tags_is_saved_with_a_revision(stack):
    mine = stack.write("I prefer short standups", tags=["work"])["id"]
    before = memory(stack, mine).json()

    response = patch(
        stack,
        mine,
        {
            "expected_revision": before["revision"],
            "memory_type": "context",
            "confidence": "high",
            "do_not_generalize": True,
            "tags": ["team"],
        },
    )

    assert response.status_code == 200, response.text
    after = memory(stack, mine).json()
    assert after["revision"] == before["revision"] + 1
    assert (after["memory_type"], after["confidence"]) == ("context", "high")
    assert (after["do_not_generalize"], after["tags"]) == (True, ["team"])
    assert after["text"] == before["text"]
    history = {item["reason"]: item["snapshot"] for item in revisions(stack, mine)}
    assert sorted(history) == ["before manual edit", "memory created"]
    # The revision is taken before the change: it holds the old values, not the new ones.
    kept = history["before manual edit"]
    assert (kept["memory_type"], kept["confidence"]) == (before["memory_type"], before["confidence"])
    assert kept["text"] == before["text"]
    assert response.json()["memory"] == after


# --- deleting ---------------------------------------------------------------


def test_another_agent_cannot_delete_a_memory(stack):
    mine = stack.write("I prefer board games on Fridays")["id"]
    before = snapshot(stack, mine)

    response = delete(stack, mine, OTHER)

    assert response.status_code == 404
    assert response.json()["detail"] == "Memory not found"
    assert snapshot(stack, mine) == before
    assert stack.index.holds(mine, "I prefer board games on Fridays")


def test_deleting_an_unknown_memory_is_not_found(stack):
    response = delete(stack, "mem_missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "Memory not found"


def test_a_delete_with_a_stale_or_malformed_revision_keeps_the_memory(stack):
    mine = stack.write("I prefer hiking in the autumn")["id"]
    before = snapshot(stack, mine)
    current = before[0]["revision"]

    stale = delete(stack, mine, expected_revision=current + 1)
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "revision_conflict", "current_revision": current}

    for revision in (0, "one"):
        malformed = delete(stack, mine, expected_revision=revision)
        assert malformed.status_code == 422, revision
        assert malformed.json()["detail"][0]["loc"] == ["query", "expected_revision"]

    assert snapshot(stack, mine) == before
    assert stack.index.holds(mine, "I prefer hiking in the autumn")


# --- conflicts --------------------------------------------------------------


def test_resolving_an_unknown_conflict_is_not_found(stack):
    old, new, _ = contradiction(stack)
    before = [snapshot(stack, old), snapshot(stack, new)]

    response = resolve(stack, "conflict_missing", new)

    assert response.status_code == 404
    assert response.json()["detail"] == "Conflict not found"
    assert [row["status"] for row in conflicts(stack)] == ["open"]
    assert [snapshot(stack, old), snapshot(stack, new)] == before


def test_another_agents_conflict_is_hidden_and_cannot_be_resolved(stack):
    old, new, conflict_id = contradiction(stack, OTHER)
    before = [snapshot(stack, old, OTHER), snapshot(stack, new, OTHER)]

    assert conflicts(stack) == []
    response = resolve(stack, conflict_id, new)

    assert response.status_code == 404
    assert response.json()["detail"] == "Conflict not found"
    assert [row["status"] for row in conflicts(stack, OTHER)] == ["open"]
    assert [snapshot(stack, old, OTHER), snapshot(stack, new, OTHER)] == before


def test_a_winner_outside_the_conflict_is_refused_and_nothing_moves(stack):
    old, new, conflict_id = contradiction(stack)
    bystander = stack.write("I prefer reading before sleep")["id"]
    before = [snapshot(stack, item) for item in (old, new, bystander)]

    for winner in (bystander, "mem_missing"):
        response = resolve(stack, conflict_id, winner)
        assert response.status_code == 422
        assert response.json()["detail"] == "Winner must be one of the conflicted memories"

    empty = resolve(stack, conflict_id, "")
    assert empty.status_code == 422
    assert empty.json()["detail"][0]["loc"] == ["body", "winner_memory_id"]

    assert [row["status"] for row in conflicts(stack)] == ["open"]
    assert [snapshot(stack, item) for item in (old, new, bystander)] == before


# --- system: backups ----------------------------------------------------------


def test_backups_list_is_empty_before_the_first_and_names_each_one_after(stack):
    assert stack.get("/system/backups").json() == {"results": []}

    created = stack.http.post("/system/backups", headers=stack.admin_headers())
    assert created.status_code == 200, created.text

    [listed] = stack.get("/system/backups").json()["results"]
    assert listed["filename"] == created.json()["filename"]
    assert listed["size"] > 0


def test_a_missing_or_foreign_backup_file_is_never_served(stack):
    folder = Path(backup_service.BACKUP_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "secrets.json").write_text('{"not": "a backup"}')
    (folder / "memorygate-notes.txt").write_text("not a backup either")
    outside = folder.parent / "memorygate-outside.json"
    outside.write_text('{"outside": "the backup folder"}')
    assert stack.get("/system/backups").json() == {"results": []}
    # A folder with a backup's name is not a file. (The list still names it: not checked here.)
    (folder / "memorygate-dir.json").mkdir()

    names = ("memorygate-missing.json", "secrets.json", "memorygate-notes.txt", "memorygate-dir.json")
    for name in names:
        response = stack.get(f"/system/backups/{name}/download")
        assert response.status_code == 404, name
        assert response.json()["detail"] == "Backup not found"

    # A well-named file next to the folder is not reachable by climbing out of it.
    for name in ("..%5Cmemorygate-outside.json", "..%2Fmemorygate-outside.json"):
        response = stack.get(f"/system/backups/{name}/download")
        assert response.status_code == 404, name
        assert "the backup folder" not in response.text


# --- system: model runtime ------------------------------------------------------


def stored_runtime(stack):
    with stack.sessions() as db:
        row = db.get(AiRuntimeSetting, "singleton")
        return None if row is None else (row.provider, row.model, row.api_key_encrypted)


def put_runtime(stack, body):
    return stack.http.put("/system/ai-runtime", headers=stack.admin_headers(), json=body)


KEPT = ("ollama", "kept-model", "")


def seeded_runtime(stack, monkeypatch):
    """A stored setting the refusal must keep. No api_key: storing one writes a key file."""
    monkeypatch.setattr(pi_inference, "PI_SERVICE_KEY", "")  # MemoryGate chooses its own model
    seeded = put_runtime(
        stack, {"current_key": ADMIN_KEY, "provider": "ollama", "model": "kept-model"}
    )
    assert seeded.status_code == 200, seeded.text
    assert stored_runtime(stack) == KEPT


def test_a_model_change_with_a_wrong_admin_key_is_refused_and_stores_nothing(stack, monkeypatch):
    seeded_runtime(stack, monkeypatch)

    response = put_runtime(
        stack, {"current_key": "wrong-key", "provider": "ollama", "model": "other-model"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Current admin key is invalid."
    assert stored_runtime(stack) == KEPT


def test_openai_without_a_key_is_refused_and_stores_nothing(stack, monkeypatch):
    seeded_runtime(stack, monkeypatch)

    no_key = put_runtime(
        stack, {"current_key": ADMIN_KEY, "provider": "openai", "model": "gpt-small"}
    )
    assert no_key.status_code == 400
    assert no_key.json()["detail"] == "Add an OpenAI API key before selecting OpenAI."

    cleared = put_runtime(
        stack,
        {
            "current_key": ADMIN_KEY,
            "provider": "openai",
            "model": "gpt-small",
            "api_key": "sk-test-not-a-secret",
            "clear_api_key": True,
        },
    )
    assert cleared.status_code == 400
    assert cleared.json()["detail"] == "OpenAI requires an API key."
    assert "sk-test-not-a-secret" not in cleared.text
    assert stored_runtime(stack) == KEPT


def test_an_unknown_provider_is_refused_and_stores_nothing(stack, monkeypatch):
    seeded_runtime(stack, monkeypatch)

    response = put_runtime(
        stack, {"current_key": ADMIN_KEY, "provider": "elsewhere", "model": "m"}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "provider"]
    assert stored_runtime(stack) == KEPT
