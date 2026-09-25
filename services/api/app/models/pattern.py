import uuid

from app.core.db import Base
from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class Pattern(Base):
    __tablename__ = "patterns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    pattern_name: Mapped[str] = mapped_column(String, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    observation_ids_json: Mapped[str] = mapped_column(Text, default="[]")

    instance_count: Mapped[int] = mapped_column(Integer, default=1)
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    contradiction_count: Mapped[int] = mapped_column(Integer, default=0)

    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    interpretation: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str] = mapped_column(Text, default="")

    applies_to_entity_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    context_conditions_json: Mapped[str] = mapped_column(Text, default="{}")

    status: Mapped[str] = mapped_column(String, default="candidate", index=True)  # candidate / active / deprecated / contradicted
    promoted_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_confirmed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
