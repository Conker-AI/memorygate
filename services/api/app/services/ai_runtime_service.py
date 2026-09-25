"""Provider configuration and encrypted credentials for MemoryGate's bounded AI calls."""
import contextlib
from pathlib import Path
from threading import Lock

from app.core.config import OLLAMA_MODEL, RUNTIME_SECRET_PATH
from app.models.ai_runtime_setting import AiRuntimeSetting
from cryptography.fernet import Fernet, InvalidToken

_SINGLETON_ID = "singleton"
_secret_lock = Lock()


def _fernet() -> Fernet:
    path = Path(RUNTIME_SECRET_PATH)
    with _secret_lock:
        if path.exists():
            key = path.read_bytes().strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            path.write_bytes(key)
            with contextlib.suppress(OSError):
                path.chmod(0o600)
    return Fernet(key)


def _row(db) -> AiRuntimeSetting | None:
    return db.get(AiRuntimeSetting, _SINGLETON_ID)


def get_runtime_config(db) -> dict:
    row = _row(db)
    if not row:
        return {"provider": "ollama", "model": OLLAMA_MODEL, "api_key": ""}
    key = ""
    if row.api_key_encrypted:
        try:
            key = _fernet().decrypt(row.api_key_encrypted.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError):
            key = ""
    return {"provider": row.provider, "model": row.model, "api_key": key}


def get_runtime_status(db) -> dict:
    config = get_runtime_config(db)
    return {
        "provider": config["provider"],
        "model": config["model"],
        "api_key_configured": bool(config["api_key"]),
        "generation_status": "blocked_budget" if config["provider"] == "openai" else "local",
    }


def update_runtime_config(db, provider: str, model: str, api_key: str | None, clear_api_key: bool = False) -> dict:
    row = _row(db)
    if not row:
        row = AiRuntimeSetting(id=_SINGLETON_ID, provider="ollama", model=OLLAMA_MODEL)
        db.add(row)
    row.provider = provider
    row.model = model.strip()
    if clear_api_key:
        row.api_key_encrypted = ""
    elif api_key is not None and api_key.strip():
        row.api_key_encrypted = _fernet().encrypt(api_key.strip().encode("utf-8")).decode("utf-8")
    db.commit()
    return get_runtime_status(db)
