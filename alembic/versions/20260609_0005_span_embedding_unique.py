"""span embedding uniqueness: idempotent span persistence (G1.9)

Revision ID: 0005_span_embedding_unique
Revises: 0004_schema_revision
Create Date: 2026-06-09

Touches AGE graph: no (relational only)

Adds a partial unique index on document_embeddings.span_id so span persistence
(core/ingest.py::persist_spans) can UPSERT the dense index row instead of
duplicating it on a re-run. Without it, re-ingesting a document would silently
accumulate duplicate vectors and corrupt future ANN retrieval (recall/scoring).

Partial (WHERE span_id IS NOT NULL): the persistence path always sets span_id
(it equals the AGE Span vertex id), but the column is nullable by design to leave
room for future document-level / level-less embeddings that have no graph node —
those are not constrained.
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_span_embedding_unique"
down_revision = "0004_schema_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_document_embeddings_span_id",
        "document_embeddings",
        ["span_id"],
        unique=True,
        postgresql_where=sa.text("span_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_document_embeddings_span_id", table_name="document_embeddings")
