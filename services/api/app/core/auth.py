from app.core.db import SessionLocal
from app.services.auth_settings_service import (
    clear_failed_attempts,
    get_lockout_status,
    register_failed_attempt,
    verify_admin_key,
    verify_agent_access_key,
)
from fastapi import Header, HTTPException, Request


def require_key(request: Request, x_memorygate_key: str | None = Header(None, alias="X-MemoryGate-Key")) -> str:
    """Admin tier. There is no unauthenticated tier: an instance with no key
    configured refuses to start rather than serving every route openly."""
    client_host = request.client.host if request.client else "unknown"
    lock_scope = f"auth:{client_host}"
    db = SessionLocal()
    try:
        remaining = get_lockout_status(lock_scope)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        if not verify_admin_key(db, x_memorygate_key):
            remaining = register_failed_attempt(lock_scope)
            if remaining:
                raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
            raise HTTPException(401, "missing or invalid X-MemoryGate-Key")
        clear_failed_attempts(lock_scope)
        return "admin"
    finally:
        db.close()


def require_read_key(request: Request, x_memorygate_key: str | None = Header(None, alias="X-MemoryGate-Key"), x_agent_id: str | None = Header(None, alias="X-Agent-Id")) -> str:
    agent_id = x_agent_id or "default"
    client_host = request.client.host if request.client else "unknown"
    lock_scope = f"read:{client_host}:{agent_id}"
    db = SessionLocal()
    try:
        remaining = get_lockout_status(lock_scope)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        if verify_admin_key(db, x_memorygate_key):
            clear_failed_attempts(lock_scope)
            return "admin"
        if verify_agent_access_key(db, x_memorygate_key, agent_id):
            clear_failed_attempts(lock_scope)
            return "read"
        remaining = register_failed_attempt(lock_scope)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        raise HTTPException(401, "missing or invalid read key")
    finally:
        db.close()
