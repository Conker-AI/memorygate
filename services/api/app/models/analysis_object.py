import uuid

from app.core.db import Base
from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class AnalysisObject(Base):
    __tablename__ = "analysis_objects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    analysis_type: Mapped[str] = mapped_column(String, index=True)
    evidence_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
