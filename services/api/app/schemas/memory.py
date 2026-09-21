from pydantic import BaseModel, Field

class MemoryWriteRequest(BaseModel):
    agent_id: str | None = None
    text: str
    source_type: str = "user"
    memory_type: str | None = None  # fact / phase / context / watch (legacy type names are transparently mapped)
    confidence: str | None = None
    do_not_generalize: bool | None = None
    review_by: str | None = None  # ISO datetime; auto-defaulted to +14d for phase memories if omitted
    tags: list[str] = []
    evidence: dict | None = None

class MemorySearchRequest(BaseModel):
    agent_id: str | None = None
    query: str

class MemoryPatchRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    text: str | None = None
    memory_type: str | None = None
    confidence: str | None = None
    do_not_generalize: bool | None = None
    review_by: str | None = None
    tags: list[str] | None = None


class ConflictResolveRequest(BaseModel):
    winner_memory_id: str = Field(min_length=1)

class MemoryResponse(BaseModel):
    id: str
    text: str
    summary: str
    memory_type: str
    source_type: str
    confidence: str
    do_not_generalize: bool
    review_by: str | None
    tags: list[str]
