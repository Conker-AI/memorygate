"""Dedicated correction capability, independent of administrator/read/ingestion keys."""

import os
import re
import secrets
from typing import Annotated

from app.services import memory_corrections, memory_forgetting
from fastapi import APIRouter, Depends, Header, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

MEMORY_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"
REQUEST_ID = r"^[A-Za-z0-9_-]{16,128}$"


class CorrectionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            if request.method == "PUT":
                content = bytearray()
                async for chunk in request.stream():
                    content.extend(chunk)
                    if len(content) > 128000:
                        return JSONResponse(
                            status_code=413,
                            content={"detail": "Correction body too large"},
                        )
                request._body = bytes(content)
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422, content={"detail": "Invalid correction request"}
                )
            except SQLAlchemyError:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Correction storage unavailable"},
                )

        return bounded


def require_correction_key(
    key: str | None = Header(None, alias="X-MemoryGate-Correction-Key"),
    requested_agent: str | None = Header(None, alias="X-Agent-Id"),
):
    expected = os.environ.get("MEMORYGATE_CORRECTION_KEY", "")
    agent = os.environ.get("MEMORYGATE_CORRECTION_AGENT_ID", "")
    if len(expected) < 32 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", agent
    ):
        raise HTTPException(401, "Correction capability unavailable")
    if not key or not secrets.compare_digest(expected.encode(), key.encode()):
        raise HTTPException(401, "Invalid correction capability")
    if requested_agent is not None and requested_agent != agent:
        raise HTTPException(403, "Correction namespace mismatch")
    return agent


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    memory_id: str = Field(pattern=MEMORY_ID)
    expected_revision: int = Field(ge=1, le=2147483646)
    text: str = Field(min_length=1, max_length=16000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Provide nonblank text without NUL")
        return value


class Forget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    memory_id: str = Field(pattern=MEMORY_ID)
    expected_revision: int = Field(ge=1, le=2147483646)


router = APIRouter(
    prefix="/runtime/corrections",
    tags=["reviewed memory correction"],
    route_class=CorrectionRoute,
)


@router.get("/memories/{memory_id}")
def memory(
    memory_id: Annotated[str, Path(pattern=MEMORY_ID)],
    agent_id: str = Depends(require_correction_key),
):
    return memory_corrections.read_memory(agent_id, memory_id)


@router.put("/forget/{request_id}")
def forget(
    request_id: Annotated[str, Path(pattern=REQUEST_ID)],
    payload: Forget,
    agent_id: str = Depends(require_correction_key),
):
    """Owner-reviewed forget: removes the memory and every stored copy of its text."""
    return memory_forgetting.forget(agent_id, request_id, payload.model_dump())


@router.get("/forget/{request_id}")
def forget_receipt(
    request_id: Annotated[str, Path(pattern=REQUEST_ID)],
    agent_id: str = Depends(require_correction_key),
):
    return memory_forgetting.read_receipt(agent_id, request_id)


@router.put("/{request_id}")
def apply(
    request_id: Annotated[str, Path(pattern=REQUEST_ID)],
    payload: Correction,
    agent_id: str = Depends(require_correction_key),
):
    return memory_corrections.apply(agent_id, request_id, payload.model_dump())


@router.get("/{request_id}")
def receipt(
    request_id: Annotated[str, Path(pattern=REQUEST_ID)],
    agent_id: str = Depends(require_correction_key),
):
    return memory_corrections.read_receipt(agent_id, request_id)
