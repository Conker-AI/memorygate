"""Observation and pattern routes beyond their basic refusals.

Every call goes through the real HTTP API. Observation and pattern writes that name an unknown or
another agent's id are refused with 422 and change nothing, even fields of the same request that
were valid. Searches narrow by every filter, never match an entity id by a fragment, and stop at
50 results. With the vector index down an observation is still stored and deleted, and the reply
says what could not be checked.
"""

from datetime import UTC, datetime, timedelta

from app.models.observation import Observation
from app.models.pattern import Pattern
from app.services.qdrant_store import OBSERVATION_COLLECTION
from memory_stack import stack as stack
from route_helpers import OTHER, entity, observe, post
from sqlalchemy import select

UNKNOWN = {"code": "unknown_ids"}
UNREACHABLE = {
    "status": "degraded",
    "component": "vector_index",
    "reason": "vector index unreachable",
}


def make_pattern(stack, name, agent="owner", **body):
    response = post(stack, "/pattern/create", {"pattern_name": name, **body}, agent)
    assert response.status_code == 200, response.text
    return response.json()["pattern"]


def observation_rows(stack) -> int:
    with stack.sessions() as db:
        return len(db.scalars(select(Observation.id)).all())


# --- observations -----------------------------------------------------------


def test_the_same_observation_twice_counts_once_more_instead_of_a_new_row(stack):
    first = observe(stack, "Hums while cooking dinner")
    response = post(
        stack,
        "/observation/create",
        {"signal_type": "verbal", "description": "Hums while cooking dinner", "hypothesis": "h"},
    )
    body = response.json()
    assert response.status_code == 200 and body["deduplicated"] is True
    assert body["observation"]["id"] == first["id"]
    assert body["observation"]["confirmation_count"] == first["confirmation_count"] + 1
    assert observation_rows(stack) == 1

    theirs = observe(stack, "Hums while cooking dinner", agent=OTHER)
    assert theirs["id"] != first["id"] and theirs["agent_id"] == OTHER
    assert stack.get(f"/observation/{first['id']}").json()["confirmation_count"] == 1


def test_with_the_index_down_an_observation_is_stored_and_deleted_and_the_reply_says_so(stack):
    stack.index.down = True
    response = post(
        stack,
        "/observation/create",
        {"signal_type": "verbal", "description": "Whistles on the stairs", "hypothesis": "h"},
    )
    body = response.json()
    assert response.status_code == 200
    assert (body["dedup"], body["indexing"]) == (UNREACHABLE, UNREACHABLE)
    row = body["observation"]
    assert stack.get(f"/observation/{row['id']}").json() == row

    stack.index.down = False  # the queued write lands once the index is back
    stack.drain()
    assert row["id"] in stack.index.vectors(OBSERVATION_COLLECTION)

    stack.index.down = True
    deleted = stack.http.delete(f"/observation/{row['id']}", headers=stack.admin_headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "ok", "indexing": UNREACHABLE}
    assert stack.get(f"/observation/{row['id']}").status_code == 404
    assert observation_rows(stack) == 0

    stack.index.down = False  # and so does the queued delete
    stack.drain()
    assert row["id"] not in stack.index.vectors(OBSERVATION_COLLECTION)


def test_observation_search_applies_every_filter_and_only_to_the_agent(stack):
    person = entity(stack, "human", "Noor Haddad")
    tonal = observe(stack, "Sighs at the inbox", signal_type="tonal", entity_ids=[person["id"]])
    asked = observe(stack, "Asked about moving", needs_clarification=True)
    confirmed = observe(stack, "Laughs at puns")
    post(stack, f"/observation/{confirmed['id']}/confirm", {})
    observe(stack, "Sighs at the inbox too", agent=OTHER, signal_type="tonal")

    def found(**body):
        response = post(stack, "/observation/search", body)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["results"]]

    assert found(signal_type="tonal") == [tonal["id"]]
    assert found(status="confirmed") == [confirmed["id"]]
    assert found(needs_clarification=True) == [asked["id"]]
    assert found(entity_id=person["id"]) == [tonal["id"]]
    assert found(entity_id=person["id"][:8]) == []  # a fragment of an id matches nothing
    assert found(query="SIGHS") == [tonal["id"]]
    assert found(query="sighs", status="confirmed") == []
    assert set(found()) == {tonal["id"], asked["id"], confirmed["id"]}


def test_observation_search_returns_the_fifty_newest_matches_not_fifty_scanned_rows(stack):
    # "ent_ab" contains "ent_a", so the newest rows pass the SQL narrowing and are then rejected:
    # the cap of 50 must count results, not rows looked at.
    start = datetime.now(UTC)
    with stack.sessions() as db:
        db.add_all(
            Observation(
                id=f"obs_near_{n:02}",
                agent_id="owner",
                signal_type="verbal",
                description="near miss",
                hypothesis="h",
                entity_ids_json='["ent_ab"]',
                observed_at=start - timedelta(minutes=n),
            )
            for n in range(20)
        )
        db.add_all(
            Observation(
                id=f"obs_hit_{n:02}",
                agent_id="owner",
                signal_type="verbal",
                description=f"note {n}",
                hypothesis="h",
                entity_ids_json='["ent_a"]',
                observed_at=start - timedelta(hours=1, minutes=n),
            )
            for n in range(55)
        )
        db.commit()

    response = post(stack, "/observation/search", {"entity_id": "ent_a"})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["results"]] == [
        f"obs_hit_{n:02}" for n in range(50)
    ]


def test_session_context_exposes_matching_unconfirmed_observations_of_the_agent_only(stack):
    mine = observe(stack, "Tense about the budget", trigger_context="budget meeting")
    unrelated = observe(stack, "Likes rain", trigger_context="weather")
    theirs = observe(stack, "Their tension", agent=OTHER, trigger_context="budget meeting")

    reply = post(
        stack,
        "/observation/session-context",
        {"session_context": "We have the budget meeting at ten."},
    )
    assert reply.status_code == 200
    assert reply.json() == {"status": "ok", "exposed": [mine["id"]], "archived": []}
    assert stack.get(f"/observation/{mine['id']}").json()["exposure_count"] == 1
    assert stack.get(f"/observation/{unrelated['id']}").json() == unrelated
    assert stack.get(f"/observation/{theirs['id']}", agent=OTHER).json() == theirs


def test_an_observation_update_changes_every_field_it_names(stack):
    mine = observe(stack, "Cycles to work")
    linked = observe(stack, "Bought a new helmet")
    body = {
        "status": "confirmed",
        "hypothesis": "commutes by bike",
        "hypothesis_confidence": 0.8,
        "confirmed_by": "owner",
        "trigger_context": "commute",
        "raise_condition": "on rain",
        "needs_clarification": True,
        "related_observation_ids": [linked["id"]],
    }
    response = post(stack, f"/observation/update/{mine['id']}", body)
    assert response.status_code == 200
    row = stack.get(f"/observation/{mine['id']}").json()
    assert {key: row[key] for key in body if key != "related_observation_ids"} == {
        key: value for key, value in body.items() if key != "related_observation_ids"
    }
    assert row["related_observation_ids"] == [linked["id"]]


def test_an_observation_update_naming_an_unknown_or_foreign_observation_changes_nothing(stack):
    mine = observe(stack, "Bakes bread on Saturdays")
    theirs = observe(stack, "Their bread", agent=OTHER)
    for ids in (["obs_missing"], [theirs["id"]]):
        response = post(
            stack,
            f"/observation/update/{mine['id']}",
            {"status": "confirmed", "hypothesis": "changed", "related_observation_ids": ids},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == {
            **UNKNOWN,
            "field": "related_observation_ids",
            "ids": ids,
        }
    assert stack.get(f"/observation/{mine['id']}").json() == mine


# --- patterns ---------------------------------------------------------------


def test_a_pattern_update_changes_every_field_it_names(stack):
    mine = make_pattern(stack, "tired on Mondays")
    seen = observe(stack, "Yawned at standup")
    person = entity(stack, "human", "Lea Brandt")
    body = {
        "description": "low energy",
        "observation_ids": [seen["id"]],
        "instance_count": 4,
        "confirmation_count": 2,
        "contradiction_count": 1,
        "confidence": 0.7,
        "interpretation": "late Sundays",
        "recommended_action": "lighter Monday plans",
        "applies_to_entity_ids": [person["id"]],
        "context_conditions": {"day": "monday"},
    }
    response = post(stack, f"/pattern/update/{mine['id']}", body)
    assert response.status_code == 200
    row = stack.get(f"/pattern/{mine['id']}").json()
    assert {key: row[key] for key in body} == body


def test_a_pattern_update_naming_unknown_ids_changes_nothing(stack):
    mine = make_pattern(stack, "early riser", description="up at six")
    their_obs = observe(stack, "Their note", agent=OTHER)
    their_entity = entity(stack, "human", "Their person", agent=OTHER)
    for field, ids in (
        ("observation_ids", ["obs_missing"]),
        ("observation_ids", [their_obs["id"]]),
        ("applies_to_entity_ids", [their_entity["id"]]),
    ):
        response = post(
            stack,
            f"/pattern/update/{mine['id']}",
            {"description": "changed", "confidence": 0.1, field: ids},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == {**UNKNOWN, "field": field, "ids": ids}
    assert stack.get(f"/pattern/{mine['id']}").json() == mine


def test_pattern_search_never_matches_an_id_fragment_and_combines_words_with_status(stack):
    person = entity(stack, "human", "Ravi Shah")
    make_pattern(stack, "quiet after calls", applies_to_entity_ids=[person["id"]])
    quiet_active = make_pattern(stack, "quiet mornings", status="active")
    make_pattern(stack, "loud on Fridays", status="active")
    make_pattern(stack, "quiet mornings", agent=OTHER, status="active")

    def found(**body):
        response = post(stack, "/pattern/search", body)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["results"]]

    assert found(entity_id=person["id"][:8]) == []
    assert found(query="QUIET", status="active") == [quiet_active["id"]]


def test_pattern_search_returns_the_fifty_newest_matches_not_fifty_scanned_rows(stack):
    start = datetime.now(UTC)
    with stack.sessions() as db:
        db.add_all(
            Pattern(
                id=f"pat_near_{n:02}",
                agent_id="owner",
                pattern_name="near miss",
                applies_to_entity_ids_json='["ent_ab"]',
                updated_at=start - timedelta(minutes=n),
            )
            for n in range(20)
        )
        db.add_all(
            Pattern(
                id=f"pat_hit_{n:02}",
                agent_id="owner",
                pattern_name=f"pattern {n}",
                applies_to_entity_ids_json='["ent_a"]',
                updated_at=start - timedelta(hours=1, minutes=n),
            )
            for n in range(55)
        )
        db.commit()

    response = post(stack, "/pattern/search", {"entity_id": "ent_a"})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["results"]] == [
        f"pat_hit_{n:02}" for n in range(50)
    ]


def test_promote_counts_only_observations_naming_the_whole_entity_id(stack):
    person = entity(stack, "human", "Mira Okafor")
    for when in ("at breakfast", "at lunch", "at dinner"):
        observe(stack, f"Mentions Mira {when}", entity_ids=[person["id"]])

    fragment = post(
        stack,
        "/pattern/promote",
        {"pattern_name": "talks about Mira", "entity_id": person["id"][:8]},
    )
    assert fragment.json() == {"status": "not_enough_evidence", "matched_count": 0, "required": 3}
    assert post(stack, "/pattern/search", {}).json() == {"results": []}

    promoted = post(
        stack,
        "/pattern/promote",
        {"pattern_name": "talks about Mira", "entity_id": person["id"], "query": "mentions mira"},
    ).json()
    assert promoted["status"] == "ok"
    assert promoted["pattern"]["applies_to_entity_ids"] == [person["id"]]
