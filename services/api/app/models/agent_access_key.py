import uuid

from app.core.db import Base
from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class AgentAccessKey(Base):
    __tablename__ = "agent_access_keys"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    label: Mapped[str] = mapped_column(String, unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    key_hash: Mapped[str] = mapped_column(Text)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BootstrapAuthority(Base):
    """Retain initial authority identity independently of editable key fields."""

    __tablename__ = "bootstrap_authorities"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # No cascade: removal of a key must not enable automatic re-creation.
    key_id: Mapped[str] = mapped_column(String, nullable=False)
