import uuid

from app.core.db import Base
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, index=True)
    evidence_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String, default="ingest")
    error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
