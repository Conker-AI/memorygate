import uuid

from app.core.db import Base
from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class EvidenceObject(Base):
    __tablename__ = "evidence_objects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    source_key: Mapped[str] = mapped_column(String, index=True)
    source_type: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    normalized_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    integrity_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    processing_state: Mapped[str] = mapped_column(String, default="pending", index=True)
    invalidated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
