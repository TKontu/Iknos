"""SQLAlchemy ORM — relational tables only.

The AGE graph schema is NOT modeled here. Source of truth for AGE schema is the
migration files themselves; autogenerate cannot see graph DDL. See MIGRATIONS.md.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import TIMESTAMP, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
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
        # Backs the propositionizer's per-span idempotency lookup (G1.7,
        # Propositionizer._extracted_hash): newest extract Action for a target_span. Functional
        # (the span id lives in JSONB inputs) + partial on the extract actor; declared here so the
        # autogenerate-drift gate sees it. The id is fetched newest-first, hence the timestamp leg.
        Index(
            "ix_actions_extract_target_span",
            text("(inputs->>'target_span')"),
            text("timestamp DESC"),
            postgresql_where=text("actor = 'propositionizer'"),
        ),
        # Backs the G1.7b cross-doc reuse lookup (core/reuse.py::find_reusable_extraction): the
        # newest extract Action whose content_hash matches a never-extracted span's, so its
        # propositions can be replayed instead of paying the LLM again. Same functional+partial
        # shape as the target_span index above; the timestamp leg serves the newest-first LIMIT 1.
        # Migration 0012.
        Index(
            "ix_actions_extract_content_hash",
            text("(inputs->>'content_hash')"),
            text("timestamp DESC"),
            postgresql_where=text("actor = 'propositionizer'"),
        ),
        # Backs the §10.2 audit reach-back (G2.7, provenance.audit::producing_action): the
        # newest extract Action naming a given Fact in its outputs. Functional (the fact id
        # lives in JSONB outputs) + partial on the extract actor; declared here so the
        # autogenerate-drift gate sees it. Migration 0009.
        Index(
            "ix_actions_extract_fact",
            text("(outputs->>'fact')"),
            text("timestamp DESC"),
            postgresql_where=text("actor = 'extractor'"),
        ),
        # Back the parse + segment idempotency lookups (G1.17 R4, migration 0010): the newest
        # Action for a document_id, filtered by actor. Same functional+partial shape as the
        # propositionizer index above; the trailing timestamp leg serves the ORDER BY ... LIMIT 1.
        Index(
            "ix_actions_parse_document_id",
            text("(inputs->>'document_id')"),
            text("timestamp DESC"),
            postgresql_where=text("actor = 'parser'"),
        ),
        Index(
            "ix_actions_segment_document_id",
            text("(inputs->>'document_id')"),
            text("timestamp DESC"),
            postgresql_where=text("actor = 'segmenter'"),
        ),
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
    # Idempotency key for span persistence (G1.9): one dense row per Span vertex.
    # Partial (span_id NOT NULL) leaves future level-less / doc-level embeddings
    # unconstrained. Declared here so the migrations autogenerate-drift gate passes.
    __table_args__ = (
        Index(
            "uq_document_embeddings_span_id",
            "span_id",
            unique=True,
            postgresql_where=text("span_id IS NOT NULL"),
        ),
        # HNSW ANN index (R4) — mirrors migration 0013 so the autogenerate-drift gate passes.
        # k-NN must order by `<=>` (vector_cosine_ops) to use it; see the `embedding` column.
        Index(
            "ix_document_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_content.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    span_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )  # Matches graph node id, optional constraints since graph is in AGE
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    # k-NN must use `<=>` (cosine) to hit the HNSW index (R4, migration 0013).
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    # The embedding model that produced this vector — the ANN **vector-space identity** (G1.16).
    # Cosine across two models is meaningless, so a same-dimension model swap must be refused
    # (EmbeddingModelMismatchError) and migrated via scripts/reembed.py, never silently mixed.
    # The comment is mirrored in migration 0008 — this DB has compare_comments on, so they must
    # match exactly or the autogenerate-drift gate fails.
    model: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Embedding model id — the ANN vector-space identity (G1.16)."
    )


class PropositionEmbedding(Base):
    """Dense index over decontextualized proposition text (§4).

    Propositions are rewritten text that does not appear in the document, so they
    cannot be pooled from the cached late-chunking embeddings — they are embedded
    afresh via EmbeddingSubstrate.embed_passages. `proposition_id` matches the AGE
    node id; there is no cross-store FK since the graph lives in AGE.
    """

    __tablename__ = "proposition_embeddings"
    __table_args__ = (
        # HNSW ANN index (R4) — mirrors migration 0013 so the autogenerate-drift gate passes.
        # k-NN must order by `<=>` (vector_cosine_ops) to use it; see the `embedding` column.
        Index(
            "ix_proposition_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    proposition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_content.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # k-NN must use `<=>` (cosine) to hit the HNSW index (R4, migration 0013).
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    # The embedding model that produced this vector — the ANN vector-space identity (G1.16).
    # See DocumentEmbedding.model: a model swap is refused, not mixed into one ANN space.
    model: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Embedding model id — the ANN vector-space identity (G1.16)."
    )


class PropositionLexicalIndex(Base):
    """Sparse lexical-exact index over proposition text (§4).

    Built with the Postgres `simple` text-search config (unstemmed, no stop-words)
    so codes/acronyms survive verbatim for exact recall — not stemmed BM25 ranking.
    The GIN index on `lexemes` is declared here (and created by the migration) so
    the ORM and migrations stay drift-free.
    """

    __tablename__ = "proposition_lexical_index"
    __table_args__ = (Index("ix_proposition_lexical_lexemes", "lexemes", postgresql_using="gin"),)

    proposition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_content.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lexemes: Mapped[str] = mapped_column(TSVECTOR, nullable=False)
