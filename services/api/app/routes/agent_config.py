from app.core.db import SessionLocal
from app.schemas.agent_config import AgentConfigUpdateRequest
from app.services.agent_config_service import config_to_dict, get_or_create_config
from fastapi import APIRouter

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/{agent_id}")
def get_config(agent_id: str):
    db = SessionLocal()
    try:
        row = get_or_create_config(db, agent_id)
        return config_to_dict(row)
    finally:
        db.close()


@router.put("/{agent_id}")
def update_config(agent_id: str, payload: AgentConfigUpdateRequest):
    db = SessionLocal()
    try:
        row = get_or_create_config(db, agent_id)

        if payload.novelty_threshold is not None:
            row.novelty_threshold = payload.novelty_threshold
        if payload.value_threshold is not None:
            row.value_threshold = payload.value_threshold
        if payload.max_observations is not None:
            row.max_observations = payload.max_observations
        if payload.signal_filter_enabled is not None:
            row.signal_filter_enabled = payload.signal_filter_enabled

        db.commit()
        db.refresh(row)
        return config_to_dict(row)
    finally:
        db.close()
