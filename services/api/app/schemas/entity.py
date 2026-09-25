from typing import Any

from pydantic import BaseModel, Field


class EntityCreateRequest(BaseModel):
    agent_id: str | None = None
    entity_type: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    agent_notes: str = ""
    agent_summary: str = ""
    importance_level: str = "normal"

class EntityUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    attributes: dict[str, Any] | None = None
    agent_notes: str | None = None
    agent_summary: str | None = None
    importance_level: str | None = None
    change_reason: str = "manual update"
    triggered_by: str = "system"

class EntitySearchRequest(BaseModel):
    agent_id: str | None = None
    query: str
    entity_type: str | None = None

class EntityLinkRequest(BaseModel):
    from_entity_id: str
    to_entity_id: str
    relationship_type: str
    strength: float = 0.5
    direction: str = "directed"
    notes: str = ""
    since_when: str = ""

class EntityEventCreateRequest(BaseModel):
    entity_id: str
    event_type: str
    description: str
    emotional_weight: int = 1
    occurred_at: str = ""

class EntityMergeRequest(BaseModel):
    keep_entity_id: str
    merge_entity_id: str

class EntityUpdateByIdRequest(BaseModel):
    entity_id: str
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    attributes: dict[str, Any] | None = None
    agent_notes: str | None = None
    agent_summary: str | None = None
    importance_level: str | None = None
    change_reason: str = "manual update"
    triggered_by: str = "system"
