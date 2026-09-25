import uuid

from app.core.db import Base
from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class EvidenceSource(Base):
    __tablename__ = "evidence_sources"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    secret_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ingested_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
