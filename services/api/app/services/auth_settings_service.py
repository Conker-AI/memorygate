import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import UTC, datetime

from app.core.config import MEMORYGATE_ADMIN_KEY
from app.models.agent_access_key import AgentAccessKey, BootstrapAuthority
from app.models.auth_setting import AuthSetting
from sqlalchemy.exc import IntegrityError

_PBKDF2_ROUNDS = 200_000
_SINGLETON_ID = "singleton"
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300
_attempt_state: dict[str, dict[str, float | int]] = {}

# An environment key is a machine secret, so it is held to a length floor rather
# than to the human-password character classes `validate_new_admin_key` applies
# to a key an owner types into the dashboard.
MIN_ENV_ADMIN_KEY_LENGTH = 16

NO_ADMIN_KEY_ERROR = """MemoryGate refuses to start: no admin key is configured.

Without one, every route - including POST /system/memory-reset - would be open
to anything that can reach the port.

Fix, in the directory that holds docker-compose.yml:

    echo "MEMORYGATE_ADMIN_KEY=$(openssl rand -base64 24)" >> .env
    docker compose up -d api

The key must be at least {minimum} characters. Once the service is running you
can replace it with a database-managed key from the dashboard's Settings screen;
after that the environment variable is no longer consulted."""

SHORT_ADMIN_KEY_ERROR = """MemoryGate refuses to start: MEMORYGATE_ADMIN_KEY is too short.

It is {actual} characters; at least {minimum} are required.

Fix, in the directory that holds docker-compose.yml:

    echo "MEMORYGATE_ADMIN_KEY=$(openssl rand -base64 24)" >> .env
    docker compose up -d api"""


def _hash_key(key: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _verify_key(key: str, encoded: str) -> bool:
    try:
        salt_b64, digest_b64 = encoded.split("$", 1)
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(digest_b64.encode())
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return hmac.compare_digest(actual, expected)


def get_auth_row(db) -> AuthSetting | None:
    return db.get(AuthSetting, _SINGLETON_ID)


def get_auth_state(db) -> dict:
    row = get_auth_row(db)
    if row and row.admin_key_hash:
        return {"auth_enabled": True, "key_source": "database", "has_managed_key": True}
    if MEMORYGATE_ADMIN_KEY:
        return {"auth_enabled": True, "key_source": "environment", "has_managed_key": False}
    # Unreachable while the service is running: `assert_admin_key_configured`
    # refuses startup in this state. Reported truthfully rather than assumed.
    return {"auth_enabled": False, "key_source": "unconfigured", "has_managed_key": False}


def verify_admin_key(db, key: str | None) -> bool:
    """Fail closed. An unconfigured MemoryGate authenticates nobody.

    This used to return True when neither key source existed, which made a
    freshly composed instance fully unauthenticated. Startup now refuses that
    state outright, and this check no longer depends on it having done so.
    """
    row = get_auth_row(db)
    if row and row.admin_key_hash:
        return bool(key) and _verify_key(key, row.admin_key_hash)
    if not MEMORYGATE_ADMIN_KEY:
        return False
    return bool(key) and secrets.compare_digest(key, MEMORYGATE_ADMIN_KEY)


def assert_admin_key_configured(db) -> str:
    """Return the configured key source, or raise with the exact fix.

    Called once at startup. Secure by default, or refuse to start.
    """
    row = get_auth_row(db)
    if row and row.admin_key_hash:
        return "database"
    if not MEMORYGATE_ADMIN_KEY:
        raise RuntimeError(NO_ADMIN_KEY_ERROR.format(minimum=MIN_ENV_ADMIN_KEY_LENGTH))
    if len(MEMORYGATE_ADMIN_KEY) < MIN_ENV_ADMIN_KEY_LENGTH:
        raise RuntimeError(
            SHORT_ADMIN_KEY_ERROR.format(
                actual=len(MEMORYGATE_ADMIN_KEY), minimum=MIN_ENV_ADMIN_KEY_LENGTH
            )
        )
    return "environment"


def validate_new_admin_key(key: str) -> str | None:
    if len(key) < 14:
        return "New key must be at least 14 characters long."
    if not re.search(r"[a-z]", key):
        return "New key must include at least one lowercase letter."
    if not re.search(r"[A-Z]", key):
        return "New key must include at least one uppercase letter."
    if not re.search(r"\d", key):
        return "New key must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", key):
        return "New key must include at least one special character."
    return None


def set_admin_key(db, key: str) -> None:
    row = get_auth_row(db)
    if not row:
        row = AuthSetting(id=_SINGLETON_ID, admin_key_hash=_hash_key(key))
        db.add(row)
    else:
        row.admin_key_hash = _hash_key(key)
    db.commit()


def generate_admin_key(length: int = 24) -> str:
    return secrets.token_urlsafe(length)[:length]


def create_agent_access_key(db, label: str, agent_id: str) -> tuple[AgentAccessKey, str]:
    key = f"mg_read_{secrets.token_urlsafe(24)}"
    row = AgentAccessKey(label=label, agent_id=agent_id, key_hash=_hash_key(key))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, key


def ensure_bootstrap_agent_access_key(db, raw_key: str, agent_id: str, label: str = "AgentGate Pi") -> AgentAccessKey:
    """Configuration seeds missing authority; it never resets an owner's decision."""
    if not raw_key.startswith("mg_read_") or len(raw_key) < 24:
        raise ValueError("MEMORYGATE_BOOTSTRAP_READ_KEY must start with mg_read_ and be at least 24 characters")
    binding_id = "environment-read-key"
    binding = db.get(BootstrapAuthority, binding_id)
    if binding:
        row = db.get(AgentAccessKey, binding.key_id)
        if row is None:
            raise ValueError("Bootstrap key was removed. Disable bootstrap configuration and "
                             "provision an owner-issued read key; access was not recreated.")
        return row
    existing = db.query(AgentAccessKey).all()
    row = next((item for item in existing if item.label == label), None)
    if row is None:
        row = next((item for item in existing if _verify_key(raw_key, item.key_hash)), None)
    if row is None and existing:
        # Pre-upgrade renamed AND rotated keys have no inferable bootstrap identity.
        # Failing closed is preferable to silently minting a second authority.
        raise ValueError("Existing read keys cannot be matched to bootstrap configuration. "
                         "Disable bootstrap configuration and use an owner-issued read key.")
    try:
        if row is None:
            row = AgentAccessKey(label=label, agent_id=agent_id,
                                 key_hash=_hash_key(raw_key), revoked=False)
            db.add(row)
            db.flush()
        db.add(BootstrapAuthority(id=binding_id, key_id=row.id))
        db.commit()
    except IntegrityError:
        # Concurrent seeders share the winning identity; neither resets its fields.
        db.rollback()
        binding = db.get(BootstrapAuthority, binding_id)
        row = db.get(AgentAccessKey, binding.key_id) if binding else None
        if row is None:
            raise ValueError("Bootstrap authority changed; review read-key provisioning.") from None
    return row


def verify_agent_access_key(db, key: str | None, agent_id: str) -> bool:
    if not key:
        return False
    rows = db.query(AgentAccessKey).filter(AgentAccessKey.agent_id == agent_id, AgentAccessKey.revoked.is_(False)).all()
    for row in rows:
        if _verify_key(key, row.key_hash):
            row.last_used_at = datetime.now(UTC)
            db.commit()
            return True
    return False


def get_lockout_status(scope: str) -> int:
    state = _attempt_state.get(scope)
    if not state:
        return 0
    locked_until = float(state.get("locked_until", 0))
    now = time.time()
    if locked_until > now:
        return int(max(1, round(locked_until - now)))
    if locked_until:
        _attempt_state.pop(scope, None)
    return 0


def register_failed_attempt(scope: str) -> int:
    now = time.time()
    state = _attempt_state.get(scope, {"count": 0, "locked_until": 0.0})
    locked_until = float(state.get("locked_until", 0))
    if locked_until <= now:
        state["count"] = int(state.get("count", 0)) + 1
        state["locked_until"] = 0.0
    if int(state["count"]) >= _MAX_FAILED_ATTEMPTS:
        state["count"] = 0
        state["locked_until"] = now + _LOCKOUT_SECONDS
    _attempt_state[scope] = state
    return get_lockout_status(scope)


def clear_failed_attempts(scope: str) -> None:
    _attempt_state.pop(scope, None)
