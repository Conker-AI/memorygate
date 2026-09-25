import uuid

from app.core.db import Base
from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class EpisodeObject(Base):
    __tablename__ = "episode_objects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    title: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    episode_type: Mapped[str] = mapped_column(String, default="event", index=True)
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    occurred_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_end: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
