import uuid

from app.core.db import Base
from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    __mapper_args__ = {"version_id_col": revision}  # noqa: RUF012 - SQLAlchemy reads this class attribute

    text: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    memory_type: Mapped[str] = mapped_column(String, default="context")  # fact / phase / context / watch
    source_type: Mapped[str] = mapped_column(String, default="user")
    confidence: Mapped[str] = mapped_column(String, default="medium")
    do_not_generalize: Mapped[bool] = mapped_column(Boolean, default=False)
    review_by: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)  # required in practice for phase, optional otherwise
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    valid_from: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
