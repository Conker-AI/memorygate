"""The briefing's emotional state reads observation times with or without a zone.

SQLite hands back `observed_at` without a time zone, Postgres with one; comparing the naive
value with the aware "now" raised TypeError, so GET /briefing answered 500 for any agent with
a recent emotional observation.
"""

from datetime import UTC, datetime, timedelta

from app.models.observation import Observation
from memory_stack import OWNER
from memory_stack import stack as stack


def observe(stack, **body):
    response = stack.http.post("/observation/create", headers=stack.admin_headers(), json={
        "signal_type": "emotional", "description": "seems low", "hypothesis": "tired", **body})
    assert response.status_code == 200, response.text
    return response.json()["observation"]["id"]


def briefing(stack):
    response = stack.http.get(f"/briefing/{OWNER}", headers=stack.admin_headers())
    assert response.status_code == 200, response.text
    return response.json()


def test_a_recent_emotional_observation_reaches_the_briefing(stack):
    observe(stack)

    result = briefing(stack)

    assert (result["emotional_state"], result["mood_summary"]) == ("tired", "seems low")


def test_an_emotional_observation_older_than_a_day_is_left_out(stack):
    observation_id = observe(stack)
    with stack.sessions() as db:
        db.get(Observation, observation_id).observed_at = datetime.now(UTC) - timedelta(hours=30)
        db.commit()

    result = briefing(stack)

    assert (result["emotional_state"], result["mood_summary"]) == (None, None)
