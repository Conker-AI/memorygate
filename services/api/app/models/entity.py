import uuid

from app.core.db import Base
from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String, default="default", index=True)
    entity_type: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    attributes_json: Mapped[str] = mapped_column(Text, default="{}")
    agent_notes: Mapped[str] = mapped_column(Text, default="")
    agent_summary: Mapped[str] = mapped_column(Text, default="")
    importance_level: Mapped[str] = mapped_column(String, default="normal")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EntityEdge(Base):
    __tablename__ = "entity_edges"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    from_entity_id: Mapped[str] = mapped_column(String, index=True)
    to_entity_id: Mapped[str] = mapped_column(String, index=True)
    relationship_type: Mapped[str] = mapped_column(String, index=True)
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    direction: Mapped[str] = mapped_column(String, default="directed")
    notes: Mapped[str] = mapped_column(Text, default="")
    since_when: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntityEvent(Base):
    __tablename__ = "entity_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    description: Mapped[str] = mapped_column(Text)
    emotional_weight: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntityHistory(Base):
    __tablename__ = "entity_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String, index=True)
    changed_field: Mapped[str] = mapped_column(String, index=True)
    old_value_json: Mapped[str] = mapped_column(Text, default="null")
    new_value_json: Mapped[str] = mapped_column(Text, default="null")
    change_reason: Mapped[str] = mapped_column(Text, default="")
    triggered_by: Mapped[str] = mapped_column(String, default="system")
    snapshot_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
