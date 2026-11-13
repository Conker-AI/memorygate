"""A lockout stops wrong keys, never the right one: callers behind one address (a proxy, the
host network) are not locked out by someone else's mistakes, and old failures expire."""

from app.services import auth_settings_service
from memory_stack import stack as stack

WRONG = {"X-MemoryGate-Key": "not-the-key-at-all", "X-Agent-Id": "owner"}


def _wrong(stack):
    return stack.http.get("/memory", headers=WRONG).status_code


def test_the_right_key_passes_while_wrong_keys_from_the_same_address_are_locked_out(stack):
    assert [_wrong(stack) for _ in range(5)] == [401, 401, 401, 401, 429]

    assert stack.http.get("/memory", headers=stack.admin_headers()).status_code == 200
    # The right key cleared the count, so a later wrong key starts from one again.
    assert _wrong(stack) == 401


def test_a_wrong_key_stays_refused_while_the_address_is_locked(stack):
    for _ in range(5):
        _wrong(stack)

    assert _wrong(stack) == 429


def test_failures_older_than_the_window_no_longer_count(stack, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(auth_settings_service.time, "time", lambda: now[0])
    for _ in range(4):
        assert _wrong(stack) == 401

    now[0] += auth_settings_service._FAILURE_WINDOW_SECONDS + 1

    assert _wrong(stack) == 401
