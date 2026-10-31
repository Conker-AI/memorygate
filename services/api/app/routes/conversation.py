"""Pi-only evidence capability: its own source, never the owner admin key."""

import os
import secrets

from app.services import conversation_memory
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

MAX_CONTENT_CHARACTERS = 16000


class ConversationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                if any(error["loc"] == ("body", "content") and error["type"] == "string_too_long"
                       for error in exc.errors()):
                    return JSONResponse(status_code=413, content={"detail": {
                        "code": "CONTENT_TOO_LARGE", "retryable": False,
                        "max_content_characters": MAX_CONTENT_CHARACTERS,
                        "message": "Content exceeds the permanent ingestion limit; do not retry this payload",
                        "next_action": "Keep the original transcript and send a bounded memory payload",
                    }})
                raise
        return bounded


router = APIRouter(prefix="/runtime/conversation", tags=["conversation evidence"], route_class=ConversationRoute)


def require_conversation_key(
    key: str | None = Header(None, alias="X-MemoryGate-Conversation-Key"),
    requested_agent: str | None = Header(None, alias="X-Agent-Id"),
) -> str:
    expected = os.environ.get("MEMORYGATE_CONVERSATION_KEY", "")
    # Bytes, not str: compare_digest raises on a non-ASCII str, and a header can carry one.
    if len(expected) < 16 or not key or not secrets.compare_digest(expected.encode(), key.encode()):
        raise HTTPException(
            401,
            "Configure a dedicated MEMORYGATE_CONVERSATION_KEY; admin/read keys do not grant ingestion",
        )
    agent = os.environ.get("MEMORYGATE_CONVERSATION_AGENT_ID", "default")
    if requested_agent is not None and requested_agent != agent:
        raise HTTPException(
            403, "Match Pi's agent ID to MEMORYGATE_CONVERSATION_AGENT_ID before retrying"
        )
    return agent


class ConversationEvidence(BaseModel):
    model_config = {"extra": "forbid"}
    session_id: str = Field(pattern=r"^ses_[A-Za-z0-9_-]+$", max_length=100)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARACTERS)
    created_at: float = Field(ge=0, le=4102444800)


@router.put("/{message_id}", responses={413: {"description": "Permanent CONTENT_TOO_LARGE; retryable=false; max_content_characters=16000"}})
def ingest(
    message_id: str,
    payload: ConversationEvidence,
    agent_id: str = Depends(require_conversation_key),
):
    if not message_id.startswith("msg_") or len(message_id) > 100:
        raise HTTPException(422, "Use Pi's original stable message ID")
    return conversation_memory.ingest(agent_id, message_id, payload.model_dump())


@router.delete("/{message_id}")
def forget(message_id: str, agent_id: str = Depends(require_conversation_key)):
    if not message_id.startswith("msg_") or len(message_id) > 100:
        raise HTTPException(422, "Use Pi's original stable message ID")
    return conversation_memory.forget(agent_id, message_id)
