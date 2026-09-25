from typing import Literal

from app.schemas.runtime import AgentContextRequest
from pydantic import Field


class LibraryRequest(AgentContextRequest):
    query: str = "library"
    search: str = Field(default="", max_length=200)
    object_type: Literal["memory", "entity", "evidence", "analysis", "episode", "observation", "pattern", "transcript"] | None = None
    after: str | None = Field(default=None, min_length=1, max_length=240)
    limit: int = Field(default=25, ge=1, le=50)


class ExploreRequest(AgentContextRequest):
    query: str = "explore"
    object_type: Literal["memory", "entity", "evidence", "analysis", "episode", "observation", "pattern", "transcript"]
    object_id: str = Field(min_length=1, max_length=200)
    operation: Literal["connections", "content"] = "connections"
    relationship: str | None = Field(default=None, max_length=120)
    after: str | None = Field(default=None, max_length=240)
    limit: int = Field(default=12, ge=1, le=25)
    field: str = Field(default="summary", max_length=60)
    offset: int = Field(default=0, ge=0, le=100000000)
    characters: int = Field(default=4000, ge=1, le=16000)
