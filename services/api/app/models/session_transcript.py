import uuid
from datetime import datetime

from app.core.db import Base
from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class SessionTranscript(Base):
    """Legacy SoulGate-era transcript archive, read and delete only.

    Transcripts live only in Pi (ADR-0002), so nothing writes new rows here;
    routes/transcript.py lists, reads and deletes the rows already stored."""
    __tablename__ = "session_transcripts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    session_id: Mapped[str] = mapped_column(String, default="", index=True)  # from Hermes state.db
    transcript: Mapped[str] = mapped_column(Text)
    session_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_by_soulgate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
