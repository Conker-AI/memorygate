"""Content-free deletion identities retained for recovery replay."""
from datetime import UTC, datetime

from app.core.db import Base
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


class DeletionReceipt(Base):
    __tablename__ = "deletion_receipts"
    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_kind: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
        default=lambda: datetime.now(UTC))


class RecoveryHold(Base):
    __tablename__ = "recovery_holds"
    id: Mapped[str] = mapped_column(String, primary_key=True)


class RecoveryVerification(Base):
    __tablename__ = "recovery_verifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    evidence_digest: Mapped[str] = mapped_column(String)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def record(db, agent_id, object_kind, object_id):
    identity = (agent_id, object_kind, object_id)
    if db.get(DeletionReceipt, identity) is None:
        db.add(DeletionReceipt(agent_id=agent_id, object_kind=object_kind, object_id=object_id))
