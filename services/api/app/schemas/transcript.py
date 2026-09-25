from pydantic import BaseModel


class TranscriptCreateRequest(BaseModel):
    agent_id: str | None = None
    session_id: str = ""
    transcript: str
    session_start: str | None = None
    session_end: str | None = None
    word_count: int | None = None  # computed from transcript if omitted
