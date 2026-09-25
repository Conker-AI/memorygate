import hmac

from app.core.db import SessionLocal
from app.schemas.auth_settings import AiRuntimeUpdateRequest, MemoryResetRequest
from app.services.ai_runtime_service import get_runtime_status, update_runtime_config
from app.services.auth_settings_service import (
    clear_failed_attempts,
    get_lockout_status,
    register_failed_attempt,
    verify_admin_key,
)
from app.services.backup_service import create_backup, list_backups, resolve_backup
from app.services.memory_reset_service import reset_memory
from app.services.ollama_service import ollama_health
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(prefix="/system", tags=["system"])

# A destructive action takes a second, deliberate confirmation on top of admin
# auth: the exact phrase, typed. The admin key alone is not enough, so a script
# or a stray tab holding a valid key cannot wipe the workspace by accident.
MEMORY_RESET_PHRASE = "RESET MEMORY"


def _require_confirmation(provided: str, expected: str) -> None:
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(400, f'Type "{expected}" to confirm this destructive action.')


@router.get("/backups")
def backups():
    return {"results": list_backups()}


@router.post("/backups")
def create_system_backup():
    db = SessionLocal()
    try:
        return create_backup(db)
    finally:
        db.close()


@router.get("/backups/{filename}/download")
def download_backup(filename: str):
    try:
        path = resolve_backup(filename)
    except FileNotFoundError:
        raise HTTPException(404, "Backup not found") from None
    return FileResponse(path, media_type="application/json", filename=path.name)


def _confirm_admin(db, current_key: str, request: Request, action: str) -> None:
    client_host = request.client.host if request.client else "unknown"
    scope = f"{action}:{client_host}"
    remaining = get_lockout_status(scope)
    if remaining:
        raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
    if not verify_admin_key(db, current_key):
        remaining = register_failed_attempt(scope)
        if remaining:
            raise HTTPException(429, f"Too many failed attempts. Try again in {remaining} seconds.")
        raise HTTPException(403, "Current admin key is invalid.")
    clear_failed_attempts(scope)


@router.get("/ai-runtime")
def ai_runtime():
    db = SessionLocal()
    try:
        return {**get_runtime_status(db), "ollama": ollama_health()}
    finally:
        db.close()


@router.put("/ai-runtime")
def update_ai_runtime(payload: AiRuntimeUpdateRequest, request: Request):
    db = SessionLocal()
    try:
        _confirm_admin(db, payload.current_key, request, "ai-runtime")
        current = get_runtime_status(db)
        if payload.provider == "openai" and not (payload.api_key or current["api_key_configured"]) and not payload.clear_api_key:
            raise HTTPException(400, "Add an OpenAI API key before selecting OpenAI.")
        if payload.provider == "openai" and payload.clear_api_key:
            raise HTTPException(400, "OpenAI requires an API key.")
        return update_runtime_config(db, payload.provider, payload.model, payload.api_key, payload.clear_api_key)
    finally:
        db.close()


@router.post("/memory-reset")
def memory_reset(payload: MemoryResetRequest, request: Request):
    db = SessionLocal()
    try:
        _confirm_admin(db, payload.current_key, request, "memory-reset")
        _require_confirmation(payload.confirmation, MEMORY_RESET_PHRASE)
        return reset_memory(db, payload.reset_from)
    finally:
        db.close()
