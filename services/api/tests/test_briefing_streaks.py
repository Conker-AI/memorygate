"""A briefing streak belongs to one habit: its streak events are counted per entity and named
after it. They were grouped by the event's description, so two habits logging "done" once each
made a streak neither had, and one habit with differently worded events never reached one."""

from memory_stack import OWNER
from memory_stack import stack as stack


def post(stack, path, body):
    response = stack.http.post(path, headers=stack.admin_headers(), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def habit(stack, name):
    return post(stack, "/entity/create", {"entity_type": "habit", "name": name})["entity"]["id"]


def streak(stack, entity_id, description):
    post(stack, "/entity/event", {"entity_id": entity_id, "event_type": "streak", "description": description})


def streaks(stack):
    response = stack.http.get(f"/briefing/{OWNER}", headers=stack.admin_headers())
    assert response.status_code == 200, response.text
    return response.json()["active_streaks"]


def test_two_habits_with_the_same_event_words_make_no_streak(stack):
    streak(stack, habit(stack, "Meditation"), "done")
    streak(stack, habit(stack, "Morning run"), "done")

    assert streaks(stack) == []


def test_one_habit_counts_all_its_streak_events_under_its_name(stack):
    meditation = habit(stack, "Meditation")
    streak(stack, meditation, "ten minutes")
    streak(stack, meditation, "twenty minutes")
    streak(stack, habit(stack, "Morning run"), "5 km")

    assert streaks(stack) == [{"name": "Meditation", "count": 2}]
