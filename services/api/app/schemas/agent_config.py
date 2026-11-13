from pydantic import BaseModel, Field


class AgentConfigUpdateRequest(BaseModel):
    novelty_threshold: float | None = Field(default=None, ge=0, le=1)
    value_threshold: float | None = Field(default=None, ge=0, le=1)
    max_observations: int | None = Field(default=None, ge=1, le=100_000)
    signal_filter_enabled: bool | None = None
