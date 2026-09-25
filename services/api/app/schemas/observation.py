from pydantic import BaseModel, Field


class ObservationCreateRequest(BaseModel):
    agent_id: str | None = None
    session_id: str = ""
    signal_type: str
    description: str
    raw_context: str = ""
    # required: an observation without a hypothesis is nothing to confirm or
    # contradict, which is the entire point of the observation lifecycle.
    hypothesis: str = Field(min_length=1)
    hypothesis_confidence: float = 0.5
    status: str = "unconfirmed"
    confirmed_by: str = ""
    trigger_context: str = ""
    max_exposures: int = 5
    raise_condition: str = ""
    needs_clarification: bool = False
    entity_ids: list[str] = Field(default_factory=list)
    related_observation_ids: list[str] = Field(default_factory=list)

class ObservationSearchRequest(BaseModel):
    agent_id: str | None = None
    query: str = ""
    signal_type: str | None = None
    status: str | None = None
    entity_id: str | None = None
    needs_clarification: bool | None = None

class ObservationUpdateRequest(BaseModel):
    status: str | None = None
    hypothesis: str | None = Field(default=None, min_length=1)
    hypothesis_confidence: float | None = None
    confirmed_by: str | None = None
    trigger_context: str | None = None
    raise_condition: str | None = None
    needs_clarification: bool | None = None
    related_observation_ids: list[str] | None = None

class ObservationConfirmRequest(BaseModel):
    confirmed_by: str = ""

class ObservationContradictRequest(BaseModel):
    reason: str = ""

class ObservationArchiveRequest(BaseModel):
    reason: str = "manual archive"

class ObservationSessionContextRequest(BaseModel):
    agent_id: str | None = None
    session_context: str
