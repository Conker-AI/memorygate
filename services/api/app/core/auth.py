from app.core.db import SessionLocal
from app.services.auth_settings_service import (
    attempt,
    has_agent_key,
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
        valid = verify_admin_key(db, x_memorygate_key)
        remaining = attempt(lock_scope, valid)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        if not valid:
            raise HTTPException(401, "missing or invalid X-MemoryGate-Key")
        return "admin"
    finally:
        db.close()


def require_read_key(request: Request, x_memorygate_key: str | None = Header(None, alias="X-MemoryGate-Key"), x_agent_id: str | None = Header(None, alias="X-Agent-Id")) -> str:
    agent_id = x_agent_id or "default"
    client_host = request.client.host if request.client else "unknown"
    db = SessionLocal()
    try:
        # X-Agent-Id is the caller's own text: ids with no key share one bucket per host, so a
        # new id per request neither earns fresh attempts nor grows the lockout table.
        if has_agent_key(db, agent_id):
            lock_scope = f"read:{client_host}:{agent_id}"
        else:
            lock_scope = f"read-unknown-agent:{client_host}"
        if x_memorygate_key and x_memorygate_key.startswith("mg_read_"):
            # A read key never matches the admin hash: checking it first would stretch one
            # miss on every request. An admin key with this prefix still passes below.
            tier = ("read" if verify_agent_access_key(db, x_memorygate_key, agent_id)
                    else "admin" if verify_admin_key(db, x_memorygate_key) else None)
        else:
            tier = ("admin" if verify_admin_key(db, x_memorygate_key)
                    else "read" if verify_agent_access_key(db, x_memorygate_key, agent_id) else None)
        remaining = attempt(lock_scope, tier is not None)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        if tier is None:
            raise HTTPException(401, "missing or invalid read key")
        return tier
    finally:
        db.close()
