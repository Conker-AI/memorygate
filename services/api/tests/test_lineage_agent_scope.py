"""Lineage stays inside one agent: another agent's object is not found for links or reads."""

from memory_stack import stack as stack

OTHER = "companion"
CHECKLIST = "I prefer the release checklist printed on paper"


def _post(stack, path, body, agent="owner"):
    return stack.http.post(path, headers=stack.admin_headers(agent), json=body)


def _link(source_type, source_id, target_type, target_id):
    return {"source_type": source_type, "source_id": source_id,
            "target_type": target_type, "target_id": target_id, "relationship": "supports"}


def _episode(stack, agent="owner"):
    return _post(stack, "/lineage/episodes", {"title": "Release week"}, agent).json()["episode"]["id"]


def test_a_link_to_another_agents_object_is_refused_and_nothing_is_stored(stack):
    mine = stack.write(CHECKLIST)["id"]
    theirs = stack.write(CHECKLIST, agent=OTHER)["id"]
    week = _episode(stack)

    across = _post(stack, "/lineage/links", _link("memory", theirs, "episode", week))
    into = _post(stack, "/lineage/links", _link("memory", mine, "episode", _episode(stack, OTHER)))

    assert across.status_code == 404 and into.status_code == 404
    assert stack.get(f"/lineage/episode/{week}").json()["incoming"] == []
    assert stack.get(f"/lineage/memory/{theirs}", agent=OTHER).json()["outgoing"] == []


def test_another_agents_lineage_is_not_found(stack):
    theirs = stack.write(CHECKLIST, agent=OTHER)["id"]

    assert stack.get(f"/lineage/memory/{theirs}").status_code == 404
    assert stack.get(f"/lineage/memory/{theirs}", agent=OTHER).status_code == 200


def test_another_agents_link_cannot_be_deleted(stack):
    theirs = stack.write(CHECKLIST, agent=OTHER)["id"]
    week = _episode(stack, OTHER)
    link = _post(stack, "/lineage/links", _link("memory", theirs, "episode", week), OTHER).json()["link"]
    path = f"/lineage/links/{link['id']}"

    refused = stack.http.delete(path, headers=stack.admin_headers())

    assert refused.status_code == 404
    assert [row["id"] for row in stack.get(f"/lineage/episode/{week}", agent=OTHER).json()["incoming"]] == [link["id"]]
    assert stack.http.delete(path, headers=stack.admin_headers(OTHER)).status_code == 200


def test_an_episode_cannot_group_another_agents_evidence(stack):
    from app.models.evidence_object import EvidenceObject
    with stack.sessions() as db:
        db.add(EvidenceObject(id="evd_other", agent_id=OTHER, source_id="s", source_key="s",
                              source_type="manual", title="t", summary="their words"))
        db.commit()

    refused = _post(stack, "/lineage/episodes", {"title": "Mine", "evidence_ids": ["evd_other"]})

    assert refused.status_code == 404
    assert stack.get("/lineage/episodes").json() == {"results": []}
