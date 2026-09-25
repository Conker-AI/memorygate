from datetime import datetime

from app.core.db import Base
from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class AiRuntimeSetting(Base):
    """Singleton model-provider configuration; API keys remain encrypted at rest."""

    __tablename__ = "ai_runtime_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="singleton")
    provider: Mapped[str] = mapped_column(String, nullable=False, default="ollama")
    model: Mapped[str] = mapped_column(String, nullable=False, default="qwen3:4b")
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
