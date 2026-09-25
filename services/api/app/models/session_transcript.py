import uuid

from app.core.db import Base
from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class SessionTranscript(Base):
    """The 'remember everything' layer - full session transcripts, stored
    verbatim and never touched by the signal filter or deleted by anything
    in this service. The signal filter only ever decides what earns
    fast-path index access (memories/observations); this is the archive
    everything else can be reconstructed from."""
    __tablename__ = "session_transcripts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    session_id: Mapped[str] = mapped_column(String, default="", index=True)  # from Hermes state.db
    transcript: Mapped[str] = mapped_column(Text)
    session_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    session_end: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_by_soulgate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
