"""Entity routes: changes land where asked, refusals change nothing, a down index is reported.

Every call goes through the real HTTP API. Both update routes (PATCH /entity/{id} and
POST /entity/update) change only the fields they are given, keep the rest, record one history
row with the caller's reason, and re-index the entity only when its name changes. A body of the
wrong shape is refused with the field it names and leaves the entity, its history, edges and
events as they were; an unknown id is 404 and stores nothing. When the vector index is down,
creating, renaming and deleting still change the source of truth and say the index is behind;
once it is back, the index catches up. Search narrows by type and never reaches another agent.
Entity routes are admin-only: a read key is refused.
"""

from app.services.qdrant_store import ENTITY_COLLECTION
from memory_stack import embed
from memory_stack import stack as stack
from sqlalchemy import text

OTHER = "companion"
DEGRADED_INDEX = {"status": "degraded", "component": "vector_index"}


def post(stack, path, body=None, agent="owner"):
    return stack.http.post(path, headers=stack.admin_headers(agent), json=body or {})


def patch(stack, entity_id, body, agent="owner"):
    return stack.http.patch(f"/entity/{entity_id}", headers=stack.admin_headers(agent), json=body)


def entity(stack, name, entity_type="human", agent="owner", **body):
    body = {"entity_type": entity_type, "name": name, **body}
    response = post(stack, "/entity/create", body, agent)
    assert response.status_code == 200, response.text
    return response.json()["entity"]


def history(stack, entity_id):
    return stack.get(f"/entity/{entity_id}/history").json()["results"]


def refused_fields(response):
    assert response.status_code == 422, response.text
    return {item["loc"][-1] for item in response.json()["detail"]}


def count(stack, table):
    with stack.engine.connect() as db:
        return db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def indexed_name(stack, entity_id, name):
    stored = stack.index.vectors(ENTITY_COLLECTION).get(entity_id)
    return stored is not None and all(
        abs(a - b) < 1e-5 for a, b in zip(stored, embed(name), strict=True)
    )


def unchanged(stack, before):
    """The entity reads back exactly as `before`, with only its creation in its history."""
    assert stack.get(f"/entity/{before['id']}").json() == before
    assert [row["changed_field"] for row in history(stack, before["id"])] == ["entity_created"]


# --- PATCH /entity/{id} -----------------------------------------------------


def test_patch_changes_only_the_given_fields_and_records_the_reason(stack):
    maya = entity(
        stack, "Maya Rowan", description="climbing partner", tags=["friend"],
        attributes={"city": "Leeds"}, agent_notes="met at the wall",
    )
    change = {
        "tags": ["friend", "neighbour"],
        "agent_summary": "Climbs on Thursdays",
        "importance_level": "high",
        "change_reason": "owner said so",
        "triggered_by": "user",
    }

    response = patch(stack, maya["id"], change)

    assert response.status_code == 200, response.text
    assert "indexing" not in response.json()
    stored = stack.get(f"/entity/{maya['id']}").json()
    assert stored["tags"] == ["friend", "neighbour"]
    assert (stored["agent_summary"], stored["importance_level"]) == ("Climbs on Thursdays", "high")
    kept = ("name", "description", "attributes", "agent_notes", "entity_type")
    assert {key: stored[key] for key in kept} == {key: maya[key] for key in kept}
    assert response.json()["entity"]["tags"] == stored["tags"]
    latest = history(stack, maya["id"])
    updated = [row for row in latest if row["changed_field"] == "entity_updated"]
    assert len(updated) == 1
    assert (updated[0]["change_reason"], updated[0]["triggered_by"]) == ("owner said so", "user")
    assert updated[0]["old_value"]["tags"] == ["friend"]
    assert updated[0]["new_value"]["tags"] == ["friend", "neighbour"]


def test_patch_renaming_an_entity_moves_its_vector_to_the_new_name(stack):
    maya = entity(stack, "Maya Rowan")
    assert indexed_name(stack, maya["id"], "Maya Rowan")

    response = patch(stack, maya["id"], {"name": "Maya Hale", "description": "married in May"})

    assert response.status_code == 200 and "indexing" not in response.json()
    stored = stack.get(f"/entity/{maya['id']}").json()
    assert (stored["name"], stored["description"]) == ("Maya Hale", "married in May")
    assert indexed_name(stack, maya["id"], "Maya Hale")
    updated = [row for row in history(stack, maya["id"]) if row["changed_field"] == "entity_updated"]
    assert len(updated) == 1
    assert updated[0]["old_value"]["name"] == "Maya Rowan"


def test_patch_with_a_wrong_field_type_is_refused_and_changes_nothing(stack):
    maya = entity(stack, "Maya Rowan", tags=["friend"], attributes={"city": "Leeds"})

    assert refused_fields(patch(stack, maya["id"], {"tags": "friend"})) == {"tags"}
    assert refused_fields(patch(stack, maya["id"], {"attributes": ["city"]})) == {"attributes"}
    both = patch(stack, maya["id"], {"name": ["Maya"], "description": 7})
    assert refused_fields(both) == {"name", "description"}

    unchanged(stack, maya)


def test_patch_while_the_index_is_down_saves_the_rename_and_reports_it(stack):
    maya = entity(stack, "Maya Rowan")
    stack.index.down = True

    quiet = patch(stack, maya["id"], {"description": "climbing partner"})
    assert quiet.status_code == 200 and "indexing" not in quiet.json()
    renamed = patch(stack, maya["id"], {"name": "Maya Hale"})

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["indexing"].items() >= DEGRADED_INDEX.items()
    assert renamed.json()["entity"]["name"] == "Maya Hale"
    stack.index.down = False
    assert stack.get(f"/entity/{maya['id']}").json()["name"] == "Maya Hale"
    assert indexed_name(stack, maya["id"], "Maya Rowan")
    stack.drain()
    assert indexed_name(stack, maya["id"], "Maya Hale")


# --- POST /entity/update ----------------------------------------------------


def test_update_by_id_changes_only_the_given_fields_and_records_the_reason(stack):
    office = entity(stack, "Quayside Office", entity_type="place", description="third floor")
    body = {
        "entity_id": office["id"],
        "name": "Harbour Office",
        "attributes": {"floor": 3},
        "agent_notes": "moved in October",
        "change_reason": "the team moved",
    }

    response = post(stack, "/entity/update", body)

    assert response.status_code == 200, response.text
    assert "indexing" not in response.json()
    stored = stack.get(f"/entity/{office['id']}").json()
    assert (stored["name"], stored["attributes"], stored["agent_notes"]) == (
        "Harbour Office", {"floor": 3}, "moved in October",
    )
    assert (stored["description"], stored["tags"]) == ("third floor", [])
    assert indexed_name(stack, office["id"], "Harbour Office")
    updated = [row for row in history(stack, office["id"]) if row["changed_field"] == "entity_updated"]
    assert [(row["change_reason"], row["triggered_by"]) for row in updated] == [
        ("the team moved", "system")
    ]
    assert updated[0]["old_value"]["name"] == "Quayside Office"


def test_update_by_id_with_the_same_name_does_not_reindex(stack):
    office = entity(stack, "Quayside Office", entity_type="place")
    queued = stack.outbox()
    stack.index.down = True

    response = post(stack, "/entity/update", {"entity_id": office["id"], "name": "Quayside Office",
                                             "tags": ["work"]})

    assert response.status_code == 200 and "indexing" not in response.json()
    assert stack.get(f"/entity/{office['id']}").json()["tags"] == ["work"]
    assert stack.outbox() == queued
    assert all(row["status"] == "complete" for row in queued)
    changes = [row["changed_field"] for row in history(stack, office["id"])]
    assert changes.count("entity_updated") == 1


def test_update_by_id_while_the_index_is_down_saves_the_rename_and_reports_it(stack):
    office = entity(stack, "Quayside Office", entity_type="place")
    stack.index.down = True

    response = post(stack, "/entity/update", {"entity_id": office["id"], "name": "Harbour Office"})

    assert response.status_code == 200, response.text
    assert response.json()["indexing"].items() >= DEGRADED_INDEX.items()
    stack.index.down = False
    assert stack.get(f"/entity/{office['id']}").json()["name"] == "Harbour Office"
    stack.drain()
    assert indexed_name(stack, office["id"], "Harbour Office")


def test_update_by_id_without_an_id_or_with_a_bad_field_is_refused_and_changes_nothing(stack):
    office = entity(stack, "Quayside Office", entity_type="place", tags=["work"])

    assert refused_fields(post(stack, "/entity/update", {"name": "Harbour Office"})) == {"entity_id"}
    bad = post(stack, "/entity/update", {"entity_id": office["id"], "tags": {"work": True}})
    assert refused_fields(bad) == {"tags"}
    missing = post(stack, "/entity/update", {"entity_id": "ent_missing", "name": "Harbour Office"})
    assert missing.status_code == 404 and missing.json()["detail"] == "Entity not found"

    unchanged(stack, office)
    assert count(stack, "entity_history") == 1


# --- events -----------------------------------------------------------------


def test_an_event_is_stored_on_its_entity_and_listed_with_it(stack):
    maya = entity(stack, "Maya Rowan")
    body = {
        "entity_id": maya["id"],
        "event_type": "met",
        "description": "Coffee at the quay",
        "emotional_weight": 3,
        "occurred_at": "2026-11-12",
    }

    response = post(stack, "/entity/event", body)

    assert response.status_code == 200, response.text
    event = response.json()["event"]
    assert {key: event[key] for key in body} == body
    assert stack.get(f"/entity/{maya['id']}/events").json() == {"results": [event]}


def test_an_event_with_a_missing_or_wrong_field_is_refused_and_stores_nothing(stack):
    maya = entity(stack, "Maya Rowan")
    base = {"entity_id": maya["id"], "event_type": "met", "description": "Coffee at the quay"}

    no_description = {key: value for key, value in base.items() if key != "description"}
    assert refused_fields(post(stack, "/entity/event", no_description)) == {"description"}
    heavy = post(stack, "/entity/event", {**base, "emotional_weight": "heavy"})
    assert refused_fields(heavy) == {"emotional_weight"}
    missing = post(stack, "/entity/event", {**base, "entity_id": "ent_missing"})
    assert missing.status_code == 404 and missing.json()["detail"] == "Entity not found"

    assert stack.get(f"/entity/{maya['id']}/events").json() == {"results": []}
    assert count(stack, "entity_events") == 0
    unchanged(stack, maya)


def test_events_of_an_unknown_entity_are_not_found(stack):
    response = stack.get("/entity/ent_missing/events")
    assert response.status_code == 404 and response.json()["detail"] == "Entity not found"


# --- links ------------------------------------------------------------------


def test_a_link_with_an_unknown_end_or_a_bad_field_is_refused_and_stores_no_edge(stack):
    maya = entity(stack, "Maya Rowan")
    office = entity(stack, "Quayside Office", entity_type="place")
    base = {"from_entity_id": maya["id"], "to_entity_id": office["id"], "relationship_type": "works at"}

    for end in ("from_entity_id", "to_entity_id"):
        missing = post(stack, "/entity/link", {**base, end: "ent_missing"})
        assert missing.status_code == 404 and missing.json()["detail"] == "Entity not found"
    no_type = {key: value for key, value in base.items() if key != "relationship_type"}
    assert refused_fields(post(stack, "/entity/link", no_type)) == {"relationship_type"}
    strong = post(stack, "/entity/link", {**base, "strength": "strong"})
    assert refused_fields(strong) == {"strength"}

    assert count(stack, "entity_edges") == 0
    for end in (maya, office):
        assert stack.get(f"/entity/{end['id']}/edges").json() == {"results": []}


def test_a_link_keeps_the_given_strength_direction_notes_and_date(stack):
    maya = entity(stack, "Maya Rowan")
    tom = entity(stack, "Tom Rowan")
    body = {
        "from_entity_id": maya["id"],
        "to_entity_id": tom["id"],
        "relationship_type": "sibling of",
        "strength": 0.9,
        "direction": "both",
        "notes": "twins",
        "since_when": "1994",
    }

    edge = post(stack, "/entity/link", body).json()["edge"]

    assert {key: edge[key] for key in body} == body
    assert stack.get(f"/entity/{tom['id']}/edges").json() == {"results": [edge]}


# --- merge ------------------------------------------------------------------


def test_a_merge_missing_either_id_is_refused_and_keeps_both(stack):
    maya = entity(stack, "Maya Rowan", tags=["friend"])
    tom = entity(stack, "Tom Rowan")

    no_merge = post(stack, "/entity/merge", {"keep_entity_id": maya["id"]})
    assert refused_fields(no_merge) == {"merge_entity_id"}
    no_keep = post(stack, "/entity/merge", {"merge_entity_id": tom["id"]})
    assert refused_fields(no_keep) == {"keep_entity_id"}

    unchanged(stack, maya)
    unchanged(stack, tom)


# --- search -----------------------------------------------------------------


def test_search_narrows_by_type_and_matches_every_text_field(stack):
    maya = entity(stack, "Maya Rowan", description="harbour pilot")
    office = entity(stack, "Quayside Office", entity_type="place", agent_notes="by the harbour")
    boat = entity(stack, "Blue Skiff", entity_type="object", agent_summary="kept in the harbour")
    cafe = entity(stack, "Harbour Cafe", entity_type="place")
    entity(stack, "Harbour Lighthouse", entity_type="place", agent=OTHER)

    def found(**body):
        response = post(stack, "/entity/search", {"query": "harbour", **body})
        assert response.status_code == 200, response.text
        return {row["id"] for row in response.json()["results"]}

    assert found() == {maya["id"], office["id"], boat["id"], cafe["id"]}
    assert found(entity_type="place") == {office["id"], cafe["id"]}
    assert found(entity_type="habit") == set()


def test_search_without_a_query_is_refused(stack):
    entity(stack, "Maya Rowan")
    assert refused_fields(post(stack, "/entity/search", {"entity_type": "human"})) == {"query"}


# --- create and delete ------------------------------------------------------


def test_create_with_a_missing_name_or_wrong_field_type_stores_nothing(stack):
    no_name = post(stack, "/entity/create", {"entity_type": "human"})
    assert refused_fields(no_name) == {"name"}
    bad_tags = post(stack, "/entity/create", {"entity_type": "human", "name": "Maya", "tags": "x"})
    assert refused_fields(bad_tags) == {"tags"}

    assert stack.get("/entity").json() == []
    assert count(stack, "entity_history") == 0
    assert stack.outbox() == []


def test_create_while_the_index_is_down_saves_it_and_says_both_checks_are_behind(stack):
    stack.index.down = True

    response = post(stack, "/entity/create", {"entity_type": "human", "name": "Maya Rowan"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["indexing"].items() >= DEGRADED_INDEX.items()
    assert body["dedup"].items() >= DEGRADED_INDEX.items()
    again = post(stack, "/entity/create", {"entity_type": "human", "name": "maya rowan"}).json()
    assert again["deduplicated"] is True and again["entity"]["id"] == body["entity"]["id"]
    stack.index.down = False
    assert [row["id"] for row in stack.get("/entity").json()] == [body["entity"]["id"]]
    stack.drain()
    assert indexed_name(stack, body["entity"]["id"], "Maya Rowan")


def test_delete_while_the_index_is_down_removes_the_entity_and_the_vector_follows(stack):
    maya = entity(stack, "Maya Rowan")
    stack.index.down = True

    response = stack.http.delete(f"/entity/{maya['id']}", headers=stack.admin_headers())

    assert response.status_code == 200, response.text
    assert response.json()["indexing"].items() >= DEGRADED_INDEX.items()
    stack.index.down = False
    assert stack.get(f"/entity/{maya['id']}").status_code == 404
    assert maya["id"] in stack.index.vectors(ENTITY_COLLECTION)
    stack.drain()
    assert maya["id"] not in stack.index.vectors(ENTITY_COLLECTION)


# --- access -----------------------------------------------------------------


def test_a_read_key_cannot_reach_the_entity_routes(stack):
    maya = entity(stack, "Maya Rowan")
    read = stack.read_headers()
    assert stack.http.get("/context/skills?tool=x", headers=read).status_code == 200

    create = stack.http.post("/entity/create", headers=read, json={"entity_type": "human", "name": "Tom"})
    assert create.status_code == 401
    assert create.json()["detail"] == "missing or invalid X-MemoryGate-Key"
    stack.clear_lockouts()
    assert stack.http.patch(f"/entity/{maya['id']}", headers=read, json={"name": "x"}).status_code == 401
    stack.clear_lockouts()
    fetched = stack.http.get(f"/entity/{maya['id']}", headers=read)
    assert fetched.status_code == 401
    assert fetched.json()["detail"] == "missing or invalid X-MemoryGate-Key"
    stack.clear_lockouts()

    assert [row["name"] for row in stack.get("/entity").json()] == ["Maya Rowan"]
    unchanged(stack, maya)
