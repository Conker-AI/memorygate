"""Id lists written through the API name only existing rows of the same agent."""

from app.models.entity import Entity
from app.models.observation import Observation
from app.models.pattern import Pattern
from memory_stack import stack as stack


def _post(stack, path, body, agent="owner"):
    return stack.http.post(path, headers=stack.admin_headers(agent), json=body)


def _seed(stack):
    with stack.sessions() as db:
        db.add_all([
            Entity(id="ent_1", agent_id="owner", entity_type="person", name="Sam"),
            Entity(id="ent_x", agent_id="helper", entity_type="person", name="Kit"),
            Observation(id="obs_1", agent_id="owner", signal_type="verbal", description="d"),
            Pattern(id="pat_1", agent_id="owner", pattern_name="p", observation_ids_json='["obs_1"]'),
        ])
        db.commit()


def test_an_unknown_or_foreign_id_is_refused_and_nothing_is_stored(stack):
    _seed(stack)
    refused = [
        ("/observation/create", {"signal_type": "verbal", "description": "new", "hypothesis": "h", "entity_ids": ["ent_gone"]}),
        ("/observation/create", {"signal_type": "verbal", "description": "new", "hypothesis": "h", "entity_ids": ["ent_x"]}),
        ("/observation/create", {"signal_type": "verbal", "description": "new", "hypothesis": "h",
                                 "related_observation_ids": ["obs_deleted"]}),
        ("/pattern/create", {"pattern_name": "q", "observation_ids": ["obs_1", "obs_deleted"]}),
        ("/pattern/create", {"pattern_name": "q", "applies_to_entity_ids": ["ent_x"]}),
    ]
    for path, body in refused:
        response = _post(stack, path, body)
        assert response.status_code == 422, (path, body)
        assert response.json()["detail"]["code"] == "unknown_ids"
    for body in ({"observation_ids": ["obs_deleted"]}, {"applies_to_entity_ids": ["ent_gone"]}):
        assert _post(stack, "/pattern/update/pat_1", body).status_code == 422
    assert _post(stack, "/observation/update/obs_1", {"related_observation_ids": ["obs_deleted"]}).status_code == 422

    with stack.sessions() as db:
        assert db.query(Observation).count() == 1 and db.query(Pattern).count() == 1
        assert db.get(Pattern, "pat_1").observation_ids_json == '["obs_1"]'


def test_known_ids_of_the_caller_are_accepted(stack):
    _seed(stack)
    created = _post(stack, "/observation/create", {"signal_type": "verbal", "description": "seen with Sam", "hypothesis": "h",
                                                   "entity_ids": ["ent_1"], "related_observation_ids": ["obs_1"]})
    assert created.status_code == 200
    assert _post(stack, "/pattern/create", {"pattern_name": "q", "observation_ids": ["obs_1"],
                                            "applies_to_entity_ids": ["ent_1"]}).status_code == 200
