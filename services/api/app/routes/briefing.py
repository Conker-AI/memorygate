from app.core.db import SessionLocal
from app.services.briefing import build_briefing
from fastapi import APIRouter

router = APIRouter(prefix="/briefing", tags=["briefing"])


@router.get("/{agent_id}")
def get_briefing(agent_id: str):
    db = SessionLocal()
    try:
        return build_briefing(db, agent_id)
    finally:
        db.close()
