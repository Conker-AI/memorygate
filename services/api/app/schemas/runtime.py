from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IngestEventRequest(BaseModel):
    # Dedicated listener URLs already identify their source; admin /ingest can
    # still supply this field explicitly.
    source_key: str = ""
    title: str = ""
    content: str = ""
    payload: dict = {}
    occurred_at: str | None = None
    tags: list[str] = []
    integrity_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    auto_process: bool = True
    agent_id: str | None = None


class AgentContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    compact: bool = False
    scope: Literal["all", "none", "selected", "conversation"] = "all"
    memory_ids: list[str] = Field(default_factory=list, max_length=1000)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def scope_fields(self):
        if (self.scope == "selected") != bool(self.memory_ids):
            raise ValueError("Only selected scope requires memory_ids.")
        if (self.scope == "conversation") != (self.session_id is not None):
            raise ValueError("Only conversation scope requires session_id.")
        if len(set(self.memory_ids)) != len(self.memory_ids) or any(
            not value.strip() or len(value) > 200 for value in self.memory_ids
        ):
            raise ValueError("Use distinct bounded memory IDs.")
        return self

    query: str = Field(min_length=1)
    session_context: str = ""
    max_items: int = Field(default=12, ge=1, le=30)
    include_evidence: bool = False
    agent_id: str | None = None


class MemoryQuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1200)
    include_evidence: bool = False
    agent_id: str | None = None
