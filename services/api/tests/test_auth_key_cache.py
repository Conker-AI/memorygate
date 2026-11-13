"""A key that passed once is not stretched again on every request; nothing else changes.

Pi sends its read key on every turn. With a dashboard admin key set, each request ran one
PBKDF2 for the admin miss and one per read row before the match. A key that verified is now
remembered (as a per-process HMAC tag, never the key or a plain hash of it) next to the hash
it matched, so a changed or revoked key is refused at once and a wrong key always pays.
"""

import hashlib

import pytest
from app.models.agent_access_key import AgentAccessKey
from app.services import auth_settings_service
from app.services.auth_settings_service import (
    create_agent_access_key,
    set_admin_key,
    verify_admin_key,
    verify_agent_access_key,
)
from memory_stack import OWNER
from memory_stack import stack as stack

OWNER_KEY = "Owner-Key-123!x"
OTHER_KEY = "Other-Key-456!y"


@pytest.fixture
def pbkdf2_calls(monkeypatch):
    monkeypatch.setattr(auth_settings_service, "_verified", {}, raising=False)
    calls = []
    real = hashlib.pbkdf2_hmac

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    def start():
        monkeypatch.setattr(auth_settings_service.hashlib, "pbkdf2_hmac", counting)
        return calls

    return start


@pytest.fixture
def keys(stack):
    """A dashboard admin key and a second read key for the owner, so one live read row
    (the bootstrap key) does not match and is checked before the one that does."""
    with stack.sessions() as db:
        set_admin_key(db, OWNER_KEY)
        row, read = create_agent_access_key(db, "second", OWNER)
    return row.id, read


def _context(stack, key, body=None):
    return stack.http.post(
        "/runtime/context",
        headers={"X-MemoryGate-Key": key, "X-Agent-Id": OWNER},
        json={"query": "memory", **(body or {})},
    )


def test_a_repeated_right_key_is_not_stretched_again(stack, keys, pbkdf2_calls):
    _, read = keys
    calls = pbkdf2_calls()
    with stack.sessions() as db:
        assert verify_admin_key(db, OWNER_KEY)
        first = len(calls)
        assert verify_admin_key(db, OWNER_KEY)
        assert len(calls) == first

        assert verify_agent_access_key(db, read, OWNER)
        second = len(calls)
        assert verify_agent_access_key(db, read, OWNER)
        assert len(calls) == second


def test_pi_read_requests_pay_once_with_a_dashboard_admin_key(stack, keys, pbkdf2_calls):
    _, read = keys
    calls = pbkdf2_calls()
    assert _context(stack, read).status_code == 200
    before = len(calls)

    assert _context(stack, read).status_code == 200
    assert len(calls) == before


def test_a_changed_admin_key_or_revoked_read_key_is_refused_at_once(stack, keys, pbkdf2_calls):
    row_id, read = keys
    pbkdf2_calls()
    with stack.sessions() as db:
        assert verify_admin_key(db, OWNER_KEY)
        assert verify_agent_access_key(db, read, OWNER)

        set_admin_key(db, OTHER_KEY)
        assert not verify_admin_key(db, OWNER_KEY)
        assert verify_admin_key(db, OTHER_KEY)

        db.get(AgentAccessKey, row_id).revoked = True
        db.commit()
        assert not verify_agent_access_key(db, read, OWNER)
    assert _context(stack, read).status_code == 401
    assert _context(stack, OWNER_KEY).status_code == 401


def test_a_wrong_key_pays_every_time_and_is_never_remembered(stack, keys, pbkdf2_calls):
    calls = pbkdf2_calls()
    with stack.sessions() as db:
        for _ in range(3):
            before = len(calls)
            assert not verify_admin_key(db, "Wrong-Key-789!z")
            assert not verify_agent_access_key(db, "mg_read_wrong_key_not_a_real_one", OWNER)
            assert len(calls) - before >= 3  # the admin hash plus both live read rows
    assert auth_settings_service._verified == {}


def test_an_admin_key_that_looks_like_a_read_key_is_still_admin(stack, keys):
    with stack.sessions() as db:
        set_admin_key(db, "mg_read_Admin-1!xx")
    # Only the admin tier may name another agent's namespace in the body.
    response = _context(stack, "mg_read_Admin-1!xx", {"agent_id": "someone-else"})
    assert response.status_code == 200, response.text
    _, read = keys
    assert _context(stack, read, {"agent_id": "someone-else"}).status_code == 403


def test_the_cache_is_bounded(stack, keys, monkeypatch):
    monkeypatch.setattr(auth_settings_service, "_verified", {}, raising=False)
    with stack.sessions() as db:
        for number in range(auth_settings_service._VERIFIED_LIMIT + 5):
            _, read = create_agent_access_key(db, f"many-{number}", "bounded")
            assert verify_agent_access_key(db, read, "bounded")
    assert len(auth_settings_service._verified) <= auth_settings_service._VERIFIED_LIMIT
