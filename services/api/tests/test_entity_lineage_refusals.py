"""Entities and lineage: refusals name the problem, store nothing, and never reach another agent.

Every call goes through the real HTTP API with the admin key. An entity belongs to one agent:
any other agent gets 404 for it on every route, and links and merges across agents are refused
without touching either side. A lineage link or episode that names a missing object or a bad
time is refused before anything is saved.
"""

from memory_stack import stack as stack

OTHER = "companion"
CHECKLIST = "I prefer the release checklist printed on paper"


def post(stack, path, body=None, agent="owner"):
    return stack.http.post(path, headers=stack.admin_headers(agent), json=body or {})


def entity(stack, name, entity_type="human", agent="owner", **body):
    body = {"entity_type": entity_type, "name": name, **body}
    response = post(stack, "/entity/create", body, agent)
    assert response.status_code == 200, response.text
    return response.json()["entity"]


def episode(stack, title="Release week"):
    return post(stack, "/lineage/episodes", {"title": title}).json()["episode"]


def link_body(source_type, source_id, target_type, target_id, **extra):
    return {
        "source_type": source_type,
        "source_id": source_id,
        "target_type": target_type,
        "target_id": target_id,
        "relationship": "supports",
        **extra,
    }


def ids(rows):
    return [row["id"] for row in rows]


# --- entities ---------------------------------------------------------------


def test_an_unknown_entity_type_is_refused_with_the_allowed_types(stack):
    response = post(stack, "/entity/create", {"entity_type": "preference", "name": "Likes tea"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "'preference'" in detail and "'human'" in detail and "/memory" in detail
    assert stack.get("/entity").json() == []


def test_another_agents_entity_is_not_found_on_every_route(stack):
    maya = entity(stack, "Maya Rowan", tags=["friend"])
    path = f"/entity/{maya['id']}"
    other = stack.admin_headers(OTHER)

    assert stack.http.get(path, headers=other).status_code == 404
    for sub in ("edges", "events", "history"):
        assert stack.http.get(f"{path}/{sub}", headers=other).status_code == 404
    assert stack.http.patch(path, headers=other, json={"name": "Changed"}).status_code == 404
    update = {"entity_id": maya["id"], "name": "Changed"}
    assert post(stack, "/entity/update", update, OTHER).status_code == 404
    event = {"entity_id": maya["id"], "event_type": "met", "description": "x"}
    assert post(stack, "/entity/event", event, OTHER).status_code == 404
    assert stack.http.delete(path, headers=other).status_code == 404

    assert stack.get(path).json() == maya
    assert stack.get(f"{path}/events").json() == {"results": []}
    assert stack.get("/entity", agent=OTHER).json() == []


def test_a_link_to_another_agents_entity_is_refused_and_stores_no_edge(stack):
    maya = entity(stack, "Maya Rowan")
    theirs = entity(stack, "Harbour Lighthouse", entity_type="place", agent=OTHER)
    link = {
        "from_entity_id": maya["id"],
        "to_entity_id": theirs["id"],
        "relationship_type": "visits",
    }

    assert post(stack, "/entity/link", link).status_code == 404
    reverse = {**link, "from_entity_id": theirs["id"], "to_entity_id": maya["id"]}
    assert post(stack, "/entity/link", reverse).status_code == 404
    assert stack.get(f"/entity/{maya['id']}/edges").json() == {"results": []}
    assert stack.get(f"/entity/{theirs['id']}/edges", agent=OTHER).json() == {"results": []}


def test_a_link_between_own_entities_shows_on_both_ends(stack):
    maya = entity(stack, "Maya Rowan")
    office = entity(stack, "Quayside Office", entity_type="place")
    body = {
        "from_entity_id": maya["id"],
        "to_entity_id": office["id"],
        "relationship_type": "works at",
    }
    edge = post(stack, "/entity/link", body).json()["edge"]
    assert (edge["relationship_type"], edge["strength"], edge["direction"]) == (
        "works at",
        0.5,
        "directed",
    )
    for end in (maya, office):
        assert ids(stack.get(f"/entity/{end['id']}/edges").json()["results"]) == [edge["id"]]


def test_merging_an_entity_into_itself_or_across_agents_is_refused(stack):
    maya = entity(stack, "Maya Rowan")
    theirs = entity(stack, "Quentin Hale", agent=OTHER)

    def merge(keep, gone):
        return post(stack, "/entity/merge", {"keep_entity_id": keep, "merge_entity_id": gone})

    same = merge(maya["id"], maya["id"])
    assert same.status_code == 400 and "must be different" in same.json()["detail"]
    across = merge(maya["id"], theirs["id"])
    assert across.status_code == 404 and across.json()["detail"] == "entity not found"
    assert merge("ent_missing", maya["id"]).status_code == 404

    assert stack.get(f"/entity/{maya['id']}").json() == maya
    assert stack.get(f"/entity/{theirs['id']}", agent=OTHER).json() == theirs


def test_creating_the_same_entity_again_merges_tags_and_keeps_existing_fields(stack):
    first = entity(
        stack,
        "Maya Rowan",
        tags=["friend"],
        description="climbing partner",
        attributes={"city": "Leeds"},
    )
    again = post(
        stack,
        "/entity/create",
        {
            "entity_type": "human",
            "name": "  maya rowan ",
            "tags": ["friend", "neighbour"],
            "description": "someone else",
            "attributes": {"city": "York", "pet": "cat"},
        },
    ).json()

    assert again["deduplicated"] is True and again["entity"]["id"] == first["id"]
    assert again["entity"]["tags"] == ["friend", "neighbour"]
    assert again["entity"]["attributes"] == {"city": "Leeds", "pet": "cat"}  # existing keys win
    assert again["entity"]["description"] == "climbing partner"
    assert len(stack.get("/entity").json()) == 1
    history = stack.get(f"/entity/{first['id']}/history").json()["results"]
    assert {row["changed_field"] for row in history} >= {"entity_created", "entity_deduplicated"}


def test_an_unknown_entity_id_is_not_found(stack):
    for path in ("/entity/ent_missing", "/entity/ent_missing/edges", "/entity/ent_missing/history"):
        assert stack.get(path).status_code == 404
    missing = stack.http.delete("/entity/ent_missing", headers=stack.admin_headers())
    assert missing.status_code == 404


# --- lineage ----------------------------------------------------------------


def test_a_link_naming_an_unknown_type_or_a_missing_object_is_refused(stack):
    memory = stack.write(CHECKLIST)["id"]
    week = episode(stack)["id"]

    unknown = post(stack, "/lineage/links", link_body("diary", "x", "episode", week))
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "Unknown object type 'diary'"
    missing = post(stack, "/lineage/links", link_body("memory", memory, "episode", "epi_missing"))
    assert missing.status_code == 404 and "epi_missing" in missing.json()["detail"]
    too_sure = link_body("memory", memory, "episode", week, confidence=1.5)
    assert post(stack, "/lineage/links", too_sure).status_code == 422

    lineage = stack.get(f"/lineage/memory/{memory}").json()
    assert (lineage["incoming"], lineage["outgoing"]) == ([], [])
    assert stack.get("/lineage/diary/x").status_code == 422
    assert stack.get("/lineage/memory/mem_missing").status_code == 404


def test_the_same_link_twice_is_one_link_and_deleting_it_twice_is_not_found(stack):
    memory = stack.write(CHECKLIST)["id"]
    week = episode(stack)["id"]
    body = link_body("memory", memory, "episode", week, metadata={"why": "same week"})

    first = post(stack, "/lineage/links", body).json()["link"]
    second = post(stack, "/lineage/links", body).json()["link"]
    assert first["id"] == second["id"] and first["metadata"] == {"why": "same week"}
    assert ids(stack.get(f"/lineage/memory/{memory}").json()["outgoing"]) == [first["id"]]
    assert ids(stack.get(f"/lineage/episode/{week}").json()["incoming"]) == [first["id"]]

    path = f"/lineage/links/{first['id']}"
    assert stack.http.delete(path, headers=stack.admin_headers()).status_code == 200
    again = stack.http.delete(path, headers=stack.admin_headers())
    assert again.status_code == 404 and again.json()["detail"] == "Lineage link not found"
    assert stack.get(f"/lineage/memory/{memory}").json()["outgoing"] == []


def test_an_episode_with_a_bad_time_or_missing_evidence_is_refused_and_not_saved(stack):
    bad_time = post(stack, "/lineage/episodes", {"title": "Trip", "occurred_start": "last tuesday"})
    assert bad_time.status_code == 422 and "occurred_start" in bad_time.json()["detail"]
    missing = post(stack, "/lineage/episodes", {"title": "Trip", "evidence_ids": ["evd_missing"]})
    assert missing.status_code == 404 and "evd_missing" in missing.json()["detail"]
    assert post(stack, "/lineage/episodes", {"title": ""}).status_code == 422
    assert stack.get("/lineage/episodes").json() == {"results": []}


def test_an_episode_is_cleaned_and_its_time_checked_on_update(stack):
    created = post(
        stack,
        "/lineage/episodes",
        {
            "title": "  Trip north ",
            "episode_type": " Travel ",
            "status": " OPEN ",
            "tags": ["walk"],
            "occurred_start": "2026-10-31T09:00:00Z",
        },
    ).json()["episode"]
    assert (created["title"], created["episode_type"], created["status"]) == (
        "Trip north",
        "travel",
        "open",
    )
    assert created["occurred_start"].startswith("2026-10-31T09:00:00")

    path = f"/lineage/episodes/{created['id']}"
    refused = stack.http.patch(path, headers=stack.admin_headers(), json={"occurred_end": "soon"})
    assert refused.status_code == 422 and "occurred_end" in refused.json()["detail"]
    change = {
        "title": "Trip north, day two",
        "tags": ["walk", "rain"],
        "occurred_end": "2026-11-01T18:00:00Z",
    }
    updated = stack.http.patch(path, headers=stack.admin_headers(), json=change).json()["episode"]
    assert (updated["title"], updated["tags"]) == ("Trip north, day two", ["walk", "rain"])
    assert updated["occurred_end"].startswith("2026-11-01T18:00:00")


def test_another_agents_episode_cannot_be_changed_or_deleted(stack):
    mine = episode(stack)
    path = f"/lineage/episodes/{mine['id']}"
    other = stack.admin_headers(OTHER)

    assert stack.http.patch(path, headers=other, json={"title": "x"}).status_code == 404
    assert stack.http.delete(path, headers=other).status_code == 404
    assert stack.get("/lineage/episodes", agent=OTHER).json() == {"results": []}
    assert stack.get("/lineage/episodes").json()["results"] == [mine]


def test_deleting_an_episode_removes_its_links_from_both_sides(stack):
    memory = stack.write(CHECKLIST)["id"]
    week = episode(stack)["id"]
    post(stack, "/lineage/links", link_body("memory", memory, "episode", week))
    post(stack, "/lineage/links", link_body("episode", week, "memory", memory))

    deleted = stack.http.delete(f"/lineage/episodes/{week}", headers=stack.admin_headers())
    assert deleted.json() == {"status": "ok"}
    lineage = stack.get(f"/lineage/memory/{memory}").json()
    assert (lineage["incoming"], lineage["outgoing"]) == ([], [])
    assert stack.get(f"/lineage/episode/{week}").status_code == 404
