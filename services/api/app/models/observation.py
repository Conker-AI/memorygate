import uuid

from app.core.db import Base
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    session_id: Mapped[str] = mapped_column(String, default="", index=True)
    observed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    signal_type: Mapped[str] = mapped_column(String, index=True)  # verbal / tonal / behavioral / physical / timing
    description: Mapped[str] = mapped_column(Text)
    raw_context: Mapped[str] = mapped_column(Text, default="")

    hypothesis: Mapped[str] = mapped_column(Text, default="")
    hypothesis_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String, default="unconfirmed", index=True)  # unconfirmed / confirmed / contradicted / archived

    confirmed_by: Mapped[str] = mapped_column(String, default="")
    entity_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    related_observation_ids_json: Mapped[str] = mapped_column(Text, default="[]")

    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    exposure_count: Mapped[int] = mapped_column(Integer, default=0)
    max_exposures: Mapped[int] = mapped_column(Integer, default=5)
    trigger_context: Mapped[str] = mapped_column(Text, default="")
    archived_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    archive_reason: Mapped[str] = mapped_column(Text, nullable=True)

    # former pending_clarifications, merged in: an ambiguous signal queued to
    # ask about later rather than guessed at now
    raise_condition: Mapped[str] = mapped_column(Text, default="")
    needs_clarification: Mapped[bool] = mapped_column(Boolean, default=False)
