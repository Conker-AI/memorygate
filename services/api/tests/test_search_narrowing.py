"""Observation and pattern search narrow in SQL and stop at 50; they never load the whole corpus."""

from datetime import datetime, timedelta

from app.models.observation import Observation
from app.models.pattern import Pattern
from memory_stack import OWNER
from memory_stack import stack as stack
from sqlalchemy import event

START = datetime(2026, 1, 1)


def _loaded(stack, kind, call):
    rows = []

    def count(_session, instance):
        if isinstance(instance, kind):
            rows.append(instance.id)

    event.listen(stack.sessions, "loaded_as_persistent", count)
    try:
        response = call()
    finally:
        event.remove(stack.sessions, "loaded_as_persistent", count)
    return response, rows


def _post(stack, path, body):
    return stack.http.post(path, headers=stack.admin_headers(), json=body)


def test_observation_search_loads_only_rows_that_can_match_and_stops_at_50(stack):
    with stack.sessions() as db:
        db.add_all([Observation(id=f"u{n:03d}", agent_id=OWNER, signal_type="verbal", description="lunch talk",
                                hypothesis="", raw_context="", observed_at=START) for n in range(120)])
        db.add_all([Observation(id=f"g{n:03d}", agent_id=OWNER, signal_type="verbal", description="Gym at dawn",
                                hypothesis="", raw_context="", entity_ids_json='["ent_9"]' if n == 3 else "[]",
                                observed_at=START + timedelta(minutes=n)) for n in range(60)])
        db.commit()

    response, loaded = _loaded(stack, Observation, lambda: _post(stack, "/observation/search", {"query": "gym AT"}))
    results = response.json()["results"]
    assert len(results) == 50 and results[0]["id"] == "g059"
    assert not any(row.startswith("u") for row in loaded) and len(loaded) <= 100

    response = _post(stack, "/observation/search", {"entity_id": "ent_9"})
    assert [item["id"] for item in response.json()["results"]] == ["g003"]


def test_pattern_search_loads_only_rows_that_can_match(stack):
    with stack.sessions() as db:
        db.add_all([Pattern(id=f"p{n:03d}", agent_id=OWNER, pattern_name=f"lunch {n}", description="",
                            interpretation="", recommended_action="", status="candidate") for n in range(80)])
        db.add(Pattern(id="hit", agent_id=OWNER, pattern_name="Quiet evenings", description="", interpretation="",
                       recommended_action="", status="active", applies_to_entity_ids_json='["ent_2"]'))
        db.commit()

    response, loaded = _loaded(stack, Pattern, lambda: _post(stack, "/pattern/search", {"query": "quiet"}))
    assert [item["id"] for item in response.json()["results"]] == ["hit"] and loaded == ["hit"]
    assert [i["id"] for i in _post(stack, "/pattern/search", {"status": "active"}).json()["results"]] == ["hit"]
    assert [i["id"] for i in _post(stack, "/pattern/search", {"entity_id": "ent_2"}).json()["results"]] == ["hit"]
