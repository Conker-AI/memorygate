"""Request helpers shared by the briefing, observation and pattern route tests.

Every call goes through the real HTTP API of the `stack` fixture (memory_stack.py).
"""

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



def entity(stack, entity_type, name, agent="owner", **attributes):
    body = {"entity_type": entity_type, "name": name, "attributes": attributes}
    response = post(stack, "/entity/create", body, agent)
    assert response.status_code == 200, response.text
    return response.json()["entity"]
