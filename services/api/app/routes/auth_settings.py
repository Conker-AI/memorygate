from app.core.db import SessionLocal
from app.models.agent_access_key import AgentAccessKey
from app.schemas.auth_settings import (
    AdminKeyRotateRequest,
    AdminKeyUpdateRequest,
    AgentKeyCreateRequest,
)
from app.services.auth_settings_service import (
    clear_failed_attempts,
    create_agent_access_key,
    generate_admin_key,
    get_auth_state,
    get_lockout_status,
    register_failed_attempt,
    set_admin_key,
    validate_new_admin_key,
    verify_admin_key,
)
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/settings")
def auth_settings():
    db = SessionLocal()
    try:
        return get_auth_state(db)
    finally:
        db.close()


@router.post("/change-key")
def change_key(payload: AdminKeyUpdateRequest, request: Request):
    client_host = request.client.host if request.client else "unknown"
    lock_scope = f"settings:{client_host}"
    db = SessionLocal()
    try:
        remaining = get_lockout_status(lock_scope)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        if not verify_admin_key(db, payload.current_key):
            remaining = register_failed_attempt(lock_scope)
            if remaining:
                raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
            raise HTTPException(403, "current key is invalid")
        validation_error = validate_new_admin_key(payload.new_key)
        if validation_error:
            raise HTTPException(400, validation_error)
        set_admin_key(db, payload.new_key)
        clear_failed_attempts(lock_scope)
        return {"status": "ok"}
    finally:
        db.close()


@router.post("/rotate-key")
def rotate_key(payload: AdminKeyRotateRequest, request: Request):
    client_host = request.client.host if request.client else "unknown"
    lock_scope = f"settings:{client_host}"
    db = SessionLocal()
    try:
        remaining = get_lockout_status(lock_scope)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        if not verify_admin_key(db, payload.current_key):
            remaining = register_failed_attempt(lock_scope)
            if remaining:
                raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
            raise HTTPException(403, "current key is invalid")
        new_key = generate_admin_key(payload.length)
        set_admin_key(db, new_key)
        clear_failed_attempts(lock_scope)
        return {"status": "ok", "new_key": new_key}
    finally:
        db.close()


@router.get("/agent-keys")
def list_agent_keys():
    db = SessionLocal()
    try:
        rows = db.query(AgentAccessKey).order_by(AgentAccessKey.created_at.desc()).all()
        return {"results": [{"id": row.id, "label": row.label, "agent_id": row.agent_id, "revoked": row.revoked, "created_at": row.created_at, "last_used_at": row.last_used_at} for row in rows]}
    finally:
        db.close()


@router.post("/agent-keys")
def create_agent_key(payload: AgentKeyCreateRequest):
    db = SessionLocal()
    try:
        if db.query(AgentAccessKey).filter(AgentAccessKey.label == payload.label).first():
            raise HTTPException(409, "An agent key with that label already exists")
        row, key = create_agent_access_key(db, payload.label, payload.agent_id)
        return {"id": row.id, "label": row.label, "agent_id": row.agent_id, "key": key}
    finally:
        db.close()


@router.post("/agent-keys/{key_id}/revoke")
def revoke_agent_key(key_id: str):
    db = SessionLocal()
    try:
        row = db.get(AgentAccessKey, key_id)
        if not row:
            raise HTTPException(404, "Agent key not found")
        row.revoked = True
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()
