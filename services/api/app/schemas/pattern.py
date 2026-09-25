from typing import Any

from pydantic import BaseModel, Field


class PatternCreateRequest(BaseModel):
    agent_id: str | None = None
    pattern_name: str
    description: str = ""
    observation_ids: list[str] = Field(default_factory=list)
    instance_count: int = 1
    confirmation_count: int = 0
    contradiction_count: int = 0
    confidence: float = 0.5
    interpretation: str = ""
    recommended_action: str = ""
    applies_to_entity_ids: list[str] = Field(default_factory=list)
    context_conditions: dict[str, Any] = Field(default_factory=dict)
    status: str = "candidate"

class PatternSearchRequest(BaseModel):
    agent_id: str | None = None
    query: str = ""
    status: str | None = None
    entity_id: str | None = None

class PatternUpdateRequest(BaseModel):
    description: str | None = None
    observation_ids: list[str] | None = None
    instance_count: int | None = None
    confirmation_count: int | None = None
    contradiction_count: int | None = None
    confidence: float | None = None
    interpretation: str | None = None
    recommended_action: str | None = None
    applies_to_entity_ids: list[str] | None = None
    context_conditions: dict[str, Any] | None = None
    status: str | None = None

class PatternPromoteRequest(BaseModel):
    agent_id: str | None = None
    pattern_name: str
    query: str = ""
    entity_id: str | None = None
    min_observations: int = 3
    confidence: float = 0.75
    interpretation: str = ""
    recommended_action: str = ""

class PatternConfirmRequest(BaseModel):
    note: str = ""

class PatternContradictRequest(BaseModel):
    note: str = ""
