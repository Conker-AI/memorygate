"""Observations and patterns: another agent's rows are not found, and refused writes store nothing.

Every call goes through the real HTTP API with the admin key. An observation or pattern belongs
to one agent: any other agent asking for it by id gets 404 and the row stays exactly as its owner
left it. Confidence is capped, promotion and deprecation follow the documented counts, and a
promote request without enough matching observations creates no pattern.
"""

from app.models.entity import Entity
from app.services.pattern_promotion import (
    DEPRECATE_AT_CONTRADICTIONS,
    MAX_PATTERN_CONFIDENCE,
    PROMOTE_AT_CONFIRMATIONS,
)
from memory_stack import stack as stack

OTHER = "companion"


def post(stack, path, body=None, agent="owner"):
    return stack.http.post(path, headers=stack.admin_headers(agent), json=body or {})


def observe(stack, description, agent="owner", **body):
    response = post(
        stack,
        "/observation/create",
        {"signal_type": "verbal", "description": description, "hypothesis": "h", **body},
        agent,
    )
    assert response.status_code == 200, response.text
    return response.json()["observation"]


def make_pattern(stack, name, agent="owner", **body):
    response = post(stack, "/pattern/create", {"pattern_name": name, **body}, agent)
    assert response.status_code == 200, response.text
    return response.json()["pattern"]


# --- observations -----------------------------------------------------------


def test_observation_routes_refuse_without_the_admin_key(stack):
    for method, path in (("post", "/observation/create"), ("get", "/observation/active")):
        response = getattr(stack.http, method)(path)
        assert response.status_code == 401
    stack.clear_lockouts()
    assert stack.get("/observation/active").json() == {"results": []}


def test_an_observation_without_a_hypothesis_is_refused_and_not_stored(stack):
    body = {"signal_type": "verbal", "description": "Talks fast before deadlines", "hypothesis": ""}
    assert post(stack, "/observation/create", body).status_code == 422
    del body["hypothesis"]
    assert post(stack, "/observation/create", body).status_code == 422
    assert stack.get("/observation/active").json() == {"results": []}


def test_another_agents_observation_is_not_found_and_stays_unchanged(stack):
    mine = observe(stack, "Skips lunch on release days")
    path = f"/observation/{mine['id']}"

    assert stack.get(path, agent=OTHER).status_code == 404
    for route, body in (
        (f"/observation/update/{mine['id']}", {"status": "confirmed", "hypothesis": "changed"}),
        (f"{path}/confirm", {"confirmed_by": "other"}),
        (f"{path}/contradict", {"reason": "other"}),
        (f"{path}/archive", {"reason": "other"}),
    ):
        response = post(stack, route, body, OTHER)
        assert response.status_code == 404 and response.json()["detail"] == "Observation not found"
    assert stack.http.delete(path, headers=stack.admin_headers(OTHER)).status_code == 404

    assert stack.get(path).json() == mine
    assert stack.get("/observation/active", agent=OTHER).json() == {"results": []}


def test_an_unknown_observation_id_is_not_found(stack):
    assert stack.get("/observation/obs_missing").status_code == 404
    assert post(stack, "/observation/update/obs_missing", {"status": "x"}).status_code == 404
    missing = stack.http.delete("/observation/obs_missing", headers=stack.admin_headers())
    assert missing.status_code == 404


def test_an_update_with_an_empty_hypothesis_is_refused_and_changes_nothing(stack):
    mine = observe(stack, "Plays chess on Sunday mornings")
    response = post(
        stack, f"/observation/update/{mine['id']}", {"hypothesis": "", "status": "confirmed"}
    )
    assert response.status_code == 422
    assert stack.get(f"/observation/{mine['id']}").json() == mine


def test_confirm_contradict_and_archive_move_the_status_for_the_owner(stack):
    mine = observe(stack, "Waters the basil every evening", needs_clarification=True)
    confirmed = post(stack, f"/observation/{mine['id']}/confirm", {"confirmed_by": "owner said so"})
    row = confirmed.json()["observation"]
    assert (row["status"], row["confirmed_by"]) == ("confirmed", "owner said so")
    assert row["confirmation_count"] == mine["confirmation_count"] + 1

    def active(flag):
        response = stack.http.get(
            "/observation/active",
            params={"needs_clarification": flag},
            headers=stack.admin_headers(),
        )
        return [item["id"] for item in response.json()["results"]]

    assert active(True) == [mine["id"]]
    assert active(False) == []

    contradicted = post(stack, f"/observation/{mine['id']}/contradict", {"reason": "not true"})
    assert contradicted.json()["observation"]["status"] == "contradicted"
    archived = post(stack, f"/observation/{mine['id']}/archive", {"reason": "old"})
    assert archived.json()["observation"]["status"] == "archived"
    assert stack.get("/observation/active").json() == {"results": []}  # archived rows leave


def test_deleting_an_observation_removes_it_for_good(stack):
    mine = observe(stack, "Reads paper maps on hikes")
    deleted = stack.http.delete(f"/observation/{mine['id']}", headers=stack.admin_headers())
    assert deleted.status_code == 200 and deleted.json()["status"] == "ok"
    assert stack.get(f"/observation/{mine['id']}").status_code == 404
    assert "paper maps" not in stack.stored_text()


# --- patterns ---------------------------------------------------------------


def test_another_agents_pattern_is_not_found_and_stays_unchanged(stack):
    mine = make_pattern(stack, "late-night work")
    path = f"/pattern/{mine['id']}"

    assert stack.get(path, agent=OTHER).status_code == 404
    for route, body in (
        (f"/pattern/update/{mine['id']}", {"status": "active", "description": "changed"}),
        (f"{path}/confirm", {}),
        (f"{path}/contradict", {}),
    ):
        response = post(stack, route, body, OTHER)
        assert response.status_code == 404 and response.json()["detail"] == "Pattern not found"

    assert stack.get(path).json() == mine
    assert post(stack, "/pattern/search", {}, OTHER).json() == {"results": []}


def test_pattern_confidence_is_capped_on_create_and_update(stack):
    mine = make_pattern(stack, "always early", confidence=2.0)
    assert mine["confidence"] == MAX_PATTERN_CONFIDENCE
    updated = post(stack, f"/pattern/update/{mine['id']}", {"confidence": 7.5}).json()["pattern"]
    assert updated["confidence"] == MAX_PATTERN_CONFIDENCE


def test_a_pattern_is_promoted_by_confirmations_and_deprecated_by_contradictions(stack):
    mine = make_pattern(stack, "coffee before code")
    assert (mine["status"], mine["promoted_at"]) == ("candidate", None)
    confirm, contradict = f"/pattern/{mine['id']}/confirm", f"/pattern/{mine['id']}/contradict"
    for _ in range(PROMOTE_AT_CONFIRMATIONS - 1):
        assert post(stack, confirm).json()["pattern"]["status"] == "candidate"
    promoted = post(stack, confirm).json()["pattern"]
    assert promoted["status"] == "active" and promoted["promoted_at"] is not None
    assert promoted["last_confirmed_at"] is not None

    for _ in range(DEPRECATE_AT_CONTRADICTIONS - 1):
        assert post(stack, contradict).json()["pattern"]["status"] == "active"
    deprecated = post(stack, contradict).json()["pattern"]
    assert deprecated["status"] == "deprecated"
    assert deprecated["contradiction_count"] == DEPRECATE_AT_CONTRADICTIONS


def test_contradicting_a_candidate_never_deprecates_it(stack):
    mine = make_pattern(stack, "quiet mornings")
    rows = [
        post(stack, f"/pattern/{mine['id']}/contradict").json()["pattern"]
        for _ in range(DEPRECATE_AT_CONTRADICTIONS + 1)
    ]
    assert rows[-1]["contradiction_count"] == DEPRECATE_AT_CONTRADICTIONS + 1
    assert {row["status"] for row in rows} == {"candidate"}


def test_setting_a_pattern_active_stamps_its_promotion_once(stack):
    mine = make_pattern(stack, "walks after lunch")
    first = post(stack, f"/pattern/update/{mine['id']}", {"status": "active"}).json()["pattern"]
    assert first["promoted_at"] is not None
    again = post(stack, f"/pattern/update/{mine['id']}", {"status": "active"}).json()["pattern"]
    assert again["promoted_at"] == first["promoted_at"]
    assert make_pattern(stack, "born active", status="active")["promoted_at"] is not None


def test_promote_without_enough_matching_observations_creates_no_pattern(stack):
    observe(stack, "Drinks green tea at noon")
    observe(stack, "Drinks green tea after dinner")
    observe(stack, "Drinks green tea at noon on Fridays", agent=OTHER)  # not the owner's

    response = post(stack, "/pattern/promote", {"pattern_name": "green tea", "query": "green tea"})
    assert response.json() == {"status": "not_enough_evidence", "matched_count": 2, "required": 3}
    assert post(stack, "/pattern/search", {}).json() == {"results": []}


def test_promote_groups_only_the_matching_observations_of_the_caller(stack):
    with stack.sessions() as db:  # observations may only name entities that exist
        db.add_all([Entity(id=key, agent_id="owner", entity_type="place", name=key)
                    for key in ("ent_office", "ent_home", "ent_river")])
        db.commit()
    tea = [
        observe(stack, "Drinks green tea at noon", entity_ids=["ent_office"]),
        observe(stack, "Drinks green tea after dinner", entity_ids=["ent_home", "ent_office"]),
    ]
    observe(stack, "Runs along the river at dawn", entity_ids=["ent_river"])
    body = {"pattern_name": "green tea", "query": "GREEN TEA", "min_observations": 2}

    candidate = post(stack, "/pattern/promote", {**body, "confidence": 0.6}).json()["pattern"]
    assert sorted(candidate["observation_ids"]) == sorted(row["id"] for row in tea)
    assert sorted(candidate["applies_to_entity_ids"]) == ["ent_home", "ent_office"]
    assert (candidate["status"], candidate["promoted_at"]) == ("candidate", None)
    assert candidate["description"] == "Promoted from 2 observations."

    active = post(stack, "/pattern/promote", {**body, "confidence": 0.9}).json()["pattern"]
    assert active["status"] == "active" and active["promoted_at"] is not None

    by_entity = post(
        stack,
        "/pattern/promote",
        {**body, "query": "", "entity_id": "ent_home", "min_observations": 1},
    ).json()["pattern"]
    assert by_entity["observation_ids"] == [tea[1]["id"]]


def test_pattern_search_and_lists_filter_by_status_entity_words_and_agent(stack):
    with stack.sessions() as db:  # patterns may only name entities that exist
        db.add(Entity(id="ent_a", agent_id="owner", entity_type="place", name="ent_a"))
        db.commit()
    tea = make_pattern(
        stack, "green tea", interpretation="calms down", applies_to_entity_ids=["ent_a"]
    )
    run = make_pattern(stack, "river run", status="active", recommended_action="suggest a run")
    make_pattern(stack, "green tea", agent=OTHER)

    def names(body):
        results = post(stack, "/pattern/search", body).json()["results"]
        return [row["pattern_name"] for row in results]

    assert sorted(names({})) == ["green tea", "river run"]
    assert names({"status": "active"}) == ["river run"]
    assert names({"entity_id": "ent_a"}) == ["green tea"]
    assert names({"query": "CALMS"}) == ["green tea"]
    assert names({"query": "suggest"}) == ["river run"]
    assert names({"query": "nothing like this"}) == []

    active = stack.get("/pattern/active/owner").json()["results"]
    candidates = stack.get("/pattern/candidates/owner").json()["results"]
    assert [row["id"] for row in active] == [run["id"]]
    assert [row["id"] for row in candidates] == [tea["id"]]
