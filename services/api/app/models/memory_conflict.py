import uuid

from app.core.db import Base
from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class MemoryConflict(Base):
    __tablename__ = "memory_conflicts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, index=True)
    memory_id: Mapped[str] = mapped_column(String, index=True)
    conflicting_memory_id: Mapped[str] = mapped_column(String, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    resolved_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
