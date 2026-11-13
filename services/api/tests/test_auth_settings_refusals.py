"""Admin key changes, agent read keys and agent config: refusals change nothing.

Every call goes through the real HTTP API. Proven here:
- the admin key changes only with the right current key and a strong new key; a refused change
  leaves the old key working, and after a change the old key (environment or database) is
  refused everywhere while the new one works;
- wrong current keys lock the settings scope out after five tries, and the lockout ends;
- agent read keys are created once per label, are shown in full only at creation, are never
  listed with their key or hash, work only for their own agent, and stop working when revoked;
  revoking an unknown id is 404 and touches no key; a corrupt stored hash authenticates nobody;
- agent config refuses out-of-range values without storing any part of the body, and a write
  for one agent leaves every other agent's config alone.
"""

from datetime import UTC, datetime, timedelta

from app.models.agent_access_key import AgentAccessKey
from app.models.agent_config import AgentConfig
from app.models.auth_setting import AuthSetting
from app.services import auth_settings_service
from memory_stack import ADMIN_KEY, OWNER, READ_KEY
from memory_stack import stack as stack

STRONG_KEY = "Strong-new-key-2026!"
OTHER_STRONG_KEY = "Another-strong-key-77#"
DEFAULTS = {
    "novelty_threshold": 0.9,
    "value_threshold": 0.3,
    "max_observations": 150,
    "signal_filter_enabled": True,
}


def headers(key: str, agent: str = OWNER) -> dict:
    return {"X-MemoryGate-Key": key, "X-Agent-Id": agent}


def change_key(stack, current: str, new: str, admin: str = ADMIN_KEY):
    return stack.http.post(
        "/auth/change-key", headers=headers(admin), json={"current_key": current, "new_key": new}
    )


def admin_works(stack, key: str) -> bool:
    stack.clear_lockouts()
    response = stack.http.get("/auth/check", headers=headers(key))
    stack.clear_lockouts()
    return response.status_code == 200


def stored_admin_hash(stack) -> str | None:
    with stack.sessions() as db:
        row = db.get(AuthSetting, "singleton")
        return row.admin_key_hash if row else None


def context_status(stack, key: str, agent: str) -> int:
    stack.clear_lockouts()
    status = stack.runtime("context", {"query": "anything"}, key=key, agent=agent).status_code
    stack.clear_lockouts()
    return status


def key_rows(stack) -> dict[str, tuple[str, str, bool]]:
    with stack.sessions() as db:
        return {
            row.id: (row.label, row.agent_id, row.revoked) for row in db.query(AgentAccessKey).all()
        }


# --- admin key: settings and authentication ---------------------------------


def test_auth_routes_refuse_without_the_admin_key(stack):
    before = key_rows(stack)
    for method, path, body in (
        ("get", "/auth/settings", None),
        ("post", "/auth/change-key", {"current_key": ADMIN_KEY, "new_key": STRONG_KEY}),
        ("post", "/auth/rotate-key", {"current_key": ADMIN_KEY}),
        ("get", "/auth/agent-keys", None),
        ("post", "/auth/agent-keys", {"label": "sneaky", "agent_id": "sneaky"}),
    ):
        stack.clear_lockouts()
        kwargs = {"json": body} if body is not None else {}
        response = getattr(stack.http, method)(path, headers=headers(READ_KEY), **kwargs)
        assert response.status_code == 401, path
        assert response.json()["detail"] == "missing or invalid X-MemoryGate-Key"
    stack.clear_lockouts()
    assert stored_admin_hash(stack) is None
    assert key_rows(stack) == before
    assert admin_works(stack, ADMIN_KEY)


def test_settings_report_the_environment_key_until_a_managed_key_is_set(stack):
    response = stack.get("/auth/settings")
    assert response.status_code == 200
    assert response.json() == {
        "auth_enabled": True,
        "key_source": "environment",
        "has_managed_key": False,
    }

    assert change_key(stack, ADMIN_KEY, STRONG_KEY).json() == {"status": "ok"}
    response = stack.http.get("/auth/settings", headers=headers(STRONG_KEY))
    assert response.json() == {"auth_enabled": True, "key_source": "database", "has_managed_key": True}


# --- admin key: change --------------------------------------------------------


def test_a_wrong_current_key_is_refused_and_the_key_stays(stack):
    response = change_key(stack, "not-the-admin-key", STRONG_KEY)

    assert response.status_code == 403
    assert response.json()["detail"] == "current key is invalid"
    assert stored_admin_hash(stack) is None
    assert admin_works(stack, ADMIN_KEY)
    assert not admin_works(stack, STRONG_KEY)


def test_a_weak_new_key_is_refused_with_its_reason_and_the_key_stays(stack):
    cases = {
        "Short-1!": "New key must be at least 14 characters long.",
        "ALL-UPPER-KEY-2026!": "New key must include at least one lowercase letter.",
        "all-lower-key-2026!": "New key must include at least one uppercase letter.",
        "No-Digits-In-This-Key!": "New key must include at least one number.",
        "NoSpecialCharacters2026": "New key must include at least one special character.",
    }
    for weak, reason in cases.items():
        response = change_key(stack, ADMIN_KEY, weak)
        assert response.status_code == 400, weak
        assert response.json()["detail"] == reason
        assert weak not in response.text
        assert not admin_works(stack, weak)

    assert stored_admin_hash(stack) is None
    assert admin_works(stack, ADMIN_KEY)


def test_a_change_without_a_current_key_is_refused_and_not_echoed(stack):
    for body in ({"new_key": STRONG_KEY}, {"current_key": "", "new_key": STRONG_KEY}):
        response = stack.http.post("/auth/change-key", headers=stack.admin_headers(), json=body)
        assert response.status_code == 422
        assert [error["loc"] for error in response.json()["detail"]] == [["body", "current_key"]]

    assert stored_admin_hash(stack) is None
    assert not admin_works(stack, STRONG_KEY)
    assert admin_works(stack, ADMIN_KEY)


def test_after_a_change_the_environment_key_is_refused_everywhere(stack):
    assert change_key(stack, ADMIN_KEY, STRONG_KEY).json() == {"status": "ok"}

    assert admin_works(stack, STRONG_KEY)
    assert not admin_works(stack, ADMIN_KEY)
    stack.clear_lockouts()
    assert stack.get("/auth/settings").status_code == 401
    stack.clear_lockouts()
    # The old key cannot authorise the next change either, even with the new key as header.
    response = change_key(stack, ADMIN_KEY, OTHER_STRONG_KEY, admin=STRONG_KEY)
    assert response.status_code == 403
    assert response.json()["detail"] == "current key is invalid"
    assert admin_works(stack, STRONG_KEY)
    assert not admin_works(stack, OTHER_STRONG_KEY)
    # The read tier accepts the admin key too, so the old key is refused there as well.
    assert context_status(stack, ADMIN_KEY, OWNER) == 401
    assert context_status(stack, STRONG_KEY, OWNER) == 200


def test_a_second_change_replaces_the_managed_key(stack):
    assert change_key(stack, ADMIN_KEY, STRONG_KEY).status_code == 200
    first = stored_admin_hash(stack)

    response = change_key(stack, STRONG_KEY, OTHER_STRONG_KEY, admin=STRONG_KEY)

    assert response.json() == {"status": "ok"}
    stored = stored_admin_hash(stack)
    assert stored is not None and stored != first
    assert auth_settings_service._verify_key(OTHER_STRONG_KEY, stored)
    assert not auth_settings_service._verify_key(STRONG_KEY, stored)
    assert admin_works(stack, OTHER_STRONG_KEY)
    assert not admin_works(stack, STRONG_KEY)
    assert not admin_works(stack, ADMIN_KEY)
    assert context_status(stack, STRONG_KEY, OWNER) == 401
    assert context_status(stack, ADMIN_KEY, OWNER) == 401
    assert context_status(stack, OTHER_STRONG_KEY, OWNER) == 200


def test_five_wrong_current_keys_lock_the_settings_out_until_the_lockout_ends(stack, monkeypatch):
    now = [1_000_000.0]

    class Clock:
        @staticmethod
        def time():
            return now[0]

    monkeypatch.setattr(auth_settings_service, "time", Clock)
    for _ in range(auth_settings_service._MAX_FAILED_ATTEMPTS - 1):
        assert change_key(stack, "wrong-current-key", STRONG_KEY).status_code == 403

    locked = change_key(stack, "wrong-current-key", STRONG_KEY)
    assert locked.status_code == 429
    assert locked.json()["detail"] == (
        f"Too many failed attempts. Try again in {auth_settings_service._LOCKOUT_SECONDS} seconds."
    )
    assert change_key(stack, "wrong-current-key", STRONG_KEY).status_code == 429
    # Rotation shares the settings scope, so it is locked too and stores nothing.
    rotated = stack.http.post(
        "/auth/rotate-key", headers=stack.admin_headers(), json={"current_key": "wrong"}
    )
    assert rotated.status_code == 429
    assert "new_key" not in rotated.json()
    assert stored_admin_hash(stack) is None

    now[0] += auth_settings_service._LOCKOUT_SECONDS + 1
    after = change_key(stack, "wrong-current-key", STRONG_KEY)
    assert after.status_code == 403
    assert after.json()["detail"] == "current key is invalid"
    assert stored_admin_hash(stack) is None
    assert admin_works(stack, ADMIN_KEY)


def test_the_right_current_key_passes_while_the_settings_are_locked(stack):
    # The lockout counts wrong keys only: the owner is never locked out by someone else's guesses.
    for _ in range(auth_settings_service._MAX_FAILED_ATTEMPTS):
        change_key(stack, "wrong-current-key", STRONG_KEY)
    assert change_key(stack, "wrong-current-key", STRONG_KEY).status_code == 429

    response = change_key(stack, ADMIN_KEY, STRONG_KEY)

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert auth_settings_service._verify_key(STRONG_KEY, stored_admin_hash(stack))
    assert admin_works(stack, STRONG_KEY)
    assert not admin_works(stack, ADMIN_KEY)


# --- admin key: rotate --------------------------------------------------------


def test_rotate_with_a_wrong_current_key_is_refused_and_the_key_stays(stack):
    response = stack.http.post(
        "/auth/rotate-key", headers=stack.admin_headers(), json={"current_key": "wrong"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "current key is invalid"
    assert "new_key" not in response.json()
    assert stored_admin_hash(stack) is None
    assert admin_works(stack, ADMIN_KEY)


def test_rotate_refuses_a_length_out_of_bounds(stack):
    for length in (15, 49):
        response = stack.http.post(
            "/auth/rotate-key",
            headers=stack.admin_headers(),
            json={"current_key": ADMIN_KEY, "length": length},
        )
        assert response.status_code == 422
        assert [error["loc"] for error in response.json()["detail"]] == [["body", "length"]]
    assert stored_admin_hash(stack) is None
    assert admin_works(stack, ADMIN_KEY)


def test_rotate_returns_a_new_key_of_the_asked_length_and_retires_the_old_one(stack):
    current = ADMIN_KEY
    for length in (16, 32, 48):
        response = stack.http.post(
            "/auth/rotate-key",
            headers=headers(current),
            json={"current_key": current, "length": length},
        )

        assert response.status_code == 200, length
        new_key = response.json()["new_key"]
        assert response.json()["status"] == "ok" and len(new_key) == length
        assert auth_settings_service._verify_key(new_key, stored_admin_hash(stack))
        assert admin_works(stack, new_key)
        assert not admin_works(stack, current)
        settings = stack.http.get("/auth/settings", headers=headers(new_key))
        assert settings.json()["key_source"] == "database"
        current = new_key
    assert not admin_works(stack, ADMIN_KEY)


# --- agent read keys -------------------------------------------------------------


def create_key(stack, label: str, agent: str, admin: str = ADMIN_KEY):
    return stack.http.post(
        "/auth/agent-keys", headers=headers(admin), json={"label": label, "agent_id": agent}
    )


def test_a_created_key_is_shown_once_and_reads_only_its_own_agent(stack):
    response = create_key(stack, "Research helper", "researcher")

    assert response.status_code == 200
    created = response.json()
    assert set(created) == {"id", "label", "agent_id", "key"}
    assert created["label"] == "Research helper" and created["agent_id"] == "researcher"
    assert created["key"].startswith("mg_read_")

    assert context_status(stack, created["key"], "researcher") == 200
    assert context_status(stack, created["key"], OWNER) == 401
    assert context_status(stack, created["key"], "companion") == 401
    # Other agents' keys do not open this one's memory.
    assert context_status(stack, stack.companion_key, "researcher") == 401
    assert context_status(stack, READ_KEY, "researcher") == 401


def test_the_key_list_never_shows_a_key_or_its_hash(stack):
    created = create_key(stack, "Research helper", "researcher").json()

    response = stack.get("/auth/agent-keys")

    assert response.status_code == 200
    rows = response.json()["results"]
    assert {row["label"] for row in rows} == {"AgentGate Pi", "companion", "Research helper"}
    with stack.sessions() as db:
        hashes = [row.key_hash for row in db.query(AgentAccessKey).all()]
    for row in rows:
        assert set(row) == {"id", "label", "agent_id", "revoked", "created_at", "last_used_at"}
        assert row["revoked"] is False
    for secret in [created["key"], READ_KEY, stack.companion_key, *hashes]:
        assert secret not in response.text


def test_the_key_list_is_newest_first(stack):
    create_key(stack, "Research helper", "researcher")
    # SQLite stores now() to the second, so give each key its own creation time.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    order = {"companion": 0, "AgentGate Pi": 1, "Research helper": 2}
    with stack.sessions() as db:
        for row in db.query(AgentAccessKey).all():
            row.created_at = start + timedelta(minutes=order[row.label])
        db.commit()

    rows = stack.get("/auth/agent-keys").json()["results"]

    assert [row["label"] for row in rows] == ["Research helper", "AgentGate Pi", "companion"]


def test_a_duplicate_label_is_refused_and_no_second_key_exists(stack):
    first = create_key(stack, "Research helper", "researcher").json()
    before = key_rows(stack)

    response = create_key(stack, "Research helper", "intruder")

    assert response.status_code == 409
    assert response.json()["detail"] == "An agent key with that label already exists"
    assert "key" not in response.json()
    assert key_rows(stack) == before
    assert context_status(stack, first["key"], "researcher") == 200


def test_a_key_with_a_bad_label_or_agent_is_refused_and_not_stored(stack):
    before = key_rows(stack)
    for body, field in (
        ({"label": "x", "agent_id": "researcher"}, "label"),
        ({"label": "y" * 81, "agent_id": "researcher"}, "label"),
        ({"label": "Research helper", "agent_id": ""}, "agent_id"),
        ({"label": "Research helper"}, "agent_id"),
    ):
        response = stack.http.post("/auth/agent-keys", headers=stack.admin_headers(), json=body)
        assert response.status_code == 422
        assert [error["loc"] for error in response.json()["detail"]] == [["body", field]]
    assert key_rows(stack) == before


def test_a_revoked_key_is_refused_and_other_keys_keep_working(stack):
    created = create_key(stack, "Research helper", "researcher").json()
    assert context_status(stack, created["key"], "researcher") == 200

    response = stack.http.post(
        f"/auth/agent-keys/{created['id']}/revoke", headers=stack.admin_headers()
    )

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    stack.clear_lockouts()
    refused = stack.runtime("context", {"query": "anything"}, key=created["key"], agent="researcher")
    assert refused.status_code == 401
    assert refused.json()["detail"] == "missing or invalid read key"
    assert context_status(stack, stack.companion_key, "companion") == 200
    assert context_status(stack, READ_KEY, OWNER) == 200
    listed = {row["id"]: row["revoked"] for row in stack.get("/auth/agent-keys").json()["results"]}
    assert listed[created["id"]] is True
    assert sum(listed.values()) == 1


def test_revoking_an_unknown_id_is_not_found_and_revokes_nothing(stack):
    before = key_rows(stack)

    response = stack.http.post("/auth/agent-keys/no-such-key/revoke", headers=stack.admin_headers())

    assert response.status_code == 404
    assert response.json()["detail"] == "Agent key not found"
    assert key_rows(stack) == before
    assert context_status(stack, stack.companion_key, "companion") == 200


def test_a_read_key_cannot_revoke_a_key(stack):
    companion = next(
        row for row in stack.get("/auth/agent-keys").json()["results"] if row["label"] == "companion"
    )
    stack.clear_lockouts()

    response = stack.http.post(
        f"/auth/agent-keys/{companion['id']}/revoke",
        headers=headers(stack.companion_key, "companion"),
    )

    assert response.status_code == 401
    stack.clear_lockouts()
    assert key_rows(stack)[companion["id"]][2] is False
    assert context_status(stack, stack.companion_key, "companion") == 200


def test_a_read_request_without_a_key_is_refused(stack):
    response = stack.http.post(
        "/runtime/context", headers={"X-Agent-Id": OWNER}, json={"query": "anything"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or invalid read key"


def test_a_corrupt_stored_hash_authenticates_nobody(stack):
    # No "$" fails the split; "abc$d" splits but is not valid base64.
    for corrupt in ("not-a-salt-and-digest", "abc$d"):
        with stack.sessions() as db:
            row = db.query(AgentAccessKey).filter(AgentAccessKey.label == "companion").one()
            row.key_hash = corrupt
            db.commit()

        assert context_status(stack, stack.companion_key, "companion") == 401, corrupt
        assert context_status(stack, corrupt, "companion") == 401, corrupt
        assert context_status(stack, READ_KEY, OWNER) == 200


# --- agent config ------------------------------------------------------------------


def config(stack, agent: str) -> dict:
    response = stack.get(f"/config/{agent}")
    assert response.status_code == 200, response.text
    return response.json()


def put_config(stack, agent: str, body: dict, key: str = ADMIN_KEY):
    return stack.http.put(f"/config/{agent}", headers=headers(key), json=body)


def test_config_routes_refuse_without_the_admin_key(stack):
    for key in ("", READ_KEY):
        stack.clear_lockouts()
        assert stack.http.get("/config/owner", headers=headers(key)).status_code == 401
        stack.clear_lockouts()
        response = put_config(stack, "owner", {"max_observations": 3}, key=key)
        assert response.status_code == 401
        assert response.json()["detail"] == "missing or invalid X-MemoryGate-Key"
    stack.clear_lockouts()
    with stack.sessions() as db:
        assert db.query(AgentConfig).count() == 0
    assert config(stack, "owner") == {"agent_id": "owner", **DEFAULTS}


def test_an_out_of_range_value_is_refused_and_no_part_of_the_body_is_stored(stack):
    saved = put_config(stack, "owner", {"max_observations": 40, "value_threshold": 0.5})
    assert saved.status_code == 200
    expected = {**DEFAULTS, "agent_id": "owner", "max_observations": 40, "value_threshold": 0.5}
    assert saved.json() == expected

    for body, field in (
        ({"novelty_threshold": 1.5, "max_observations": 7}, "novelty_threshold"),
        ({"value_threshold": -0.1, "max_observations": 7}, "value_threshold"),
        ({"max_observations": 0, "value_threshold": 0.9}, "max_observations"),
        ({"max_observations": 100_001}, "max_observations"),
        ({"signal_filter_enabled": "maybe", "max_observations": 7}, "signal_filter_enabled"),
    ):
        response = put_config(stack, "owner", body)
        assert response.status_code == 422, body
        assert [error["loc"] for error in response.json()["detail"]] == [["body", field]]
        assert config(stack, "owner") == expected


def test_a_refused_write_for_a_new_agent_creates_no_config(stack):
    assert put_config(stack, "newcomer", {"novelty_threshold": 2}).status_code == 422
    with stack.sessions() as db:
        assert db.get(AgentConfig, "newcomer") is None


def test_a_partial_write_keeps_the_other_fields_and_other_agents_configs(stack):
    put_config(stack, "companion", {"max_observations": 12, "signal_filter_enabled": False})

    response = put_config(stack, "owner", {"novelty_threshold": 0.5})

    assert response.json() == {**DEFAULTS, "agent_id": "owner", "novelty_threshold": 0.5}
    assert config(stack, "companion") == {
        **DEFAULTS,
        "agent_id": "companion",
        "max_observations": 12,
        "signal_filter_enabled": False,
    }
    # An empty body changes nothing.
    assert put_config(stack, "owner", {}).json() == config(stack, "owner")
    assert config(stack, "owner")["novelty_threshold"] == 0.5
