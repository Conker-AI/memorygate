"""A refused request body is never sent back: a key in a bad request stays out of the reply."""

import pytest
from memory_stack import stack as stack

SECRET = "never-echo-current-key-0123456789"
PROVIDER_KEY = "sk-never-echo-provider-0123456789"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        # A missing field: pydantic's error input is the whole body, the current key included.
        ("POST", "/auth/change-key", {"current_key": SECRET}),
        ("POST", "/auth/change-key", {"current_key": SECRET, "new_key": "short"}),
        ("PUT", "/system/ai-runtime", {"current_key": SECRET, "api_key": PROVIDER_KEY}),
        # A wrong type: the error input is the value itself.
        ("POST", "/auth/rotate-key", {"current_key": [SECRET]}),
        ("POST", "/system/memory-reset", {"current_key": {"key": SECRET}}),
    ],
)
def test_refused_body_is_not_echoed(stack, method, path, body):
    response = stack.http.request(method, path, json=body, headers=stack.admin_headers())

    assert response.status_code == 422
    assert SECRET not in response.text
    assert PROVIDER_KEY not in response.text
    # Callers still learn which field was wrong and why.
    for error in response.json()["detail"]:
        assert set(error) == {"loc", "msg", "type"}
        assert error["loc"][0] == "body"
