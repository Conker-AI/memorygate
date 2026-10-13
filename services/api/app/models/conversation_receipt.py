"""Stable source identity and deletion tombstone, separate from retained content."""

from app.core.db import Base
from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class ConversationReceipt(Base):
    __tablename__ = "conversation_receipts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    message_id: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str] = mapped_column(String, default="")
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default="new")
    memory_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    value_score: Mapped[float] = mapped_column(Float, default=0.0)
    admission_method: Mapped[str | None] = mapped_column(String, nullable=True)
    index_pending: Mapped[str] = mapped_column(String, default="none")
    index_next_at: Mapped[float] = mapped_column(Float, default=0.0)
    index_error: Mapped[str] = mapped_column(Text, default="")
