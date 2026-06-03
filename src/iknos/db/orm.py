"""SQLAlchemy ORM — relational tables only.

The AGE graph schema is NOT modeled here. Source of truth for AGE schema is the
migration files themselves; autogenerate cannot see graph DDL. See MIGRATIONS.md.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, Index, Text, text, Integer, ForeignKey
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocumentContent(Base):
    __tablename__ = "document_content"

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        Index("ix_actions_timestamp", "timestamp"),
        Index("ix_actions_actor_type", "actor", "action_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    outputs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    model: Mapped[str | None] = mapped_column(Text)
    sampling: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_judgment: Mapped[str | None] = mapped_column(Text)
    calibration: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class DocumentEmbedding(Base):
    __tablename__ = "document_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_content.document_id", ondelete="CASCADE"), nullable=False
    )
    span_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # Matches graph node id, optional constraints since graph is in AGE
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
