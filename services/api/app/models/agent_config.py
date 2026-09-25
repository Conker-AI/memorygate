from app.core.db import Base
from sqlalchemy import Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column


class AgentConfig(Base):
    __tablename__ = "agent_configs"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)

    novelty_threshold: Mapped[float] = mapped_column(Float, default=0.90)
    value_threshold: Mapped[float] = mapped_column(Float, default=0.3)
    max_observations: Mapped[int] = mapped_column(Integer, default=150)
    signal_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
