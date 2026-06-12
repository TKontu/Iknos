"""baseline_chunks: the E1 plain-RAG baseline's own dense index (V4)

Revision ID: 0014_baseline_chunks
Revises: 0013_embedding_hnsw_indexes
Create Date: 2026-06-12

Touches AGE graph: no (relational only)

The E1 go/no-go (architecture.md §8, docs/todo_trials.md) needs a *fair strong* plain-RAG
baseline to measure the system against. That baseline (src/iknos/baselines/rag.py) chunks
documents into naive fixed-size token windows and retrieves top-k by cosine — deliberately
**not** iknos segmentation/propositions — so it needs its own dense index, separate from
`document_embeddings`/`proposition_embeddings`. This adds that table.

`baseline_chunks` carries: a generated uuid `id` (the citation handle in the BaselineAnswer
contract), the baseline's own `document_id` (no FK to `document_content` — a baseline run need
not populate the pipeline tables), the `chunk_index`/`char_start`/`char_end` that make a chunk
traceable to its source text, the chunk `text`, the 1024-d `embedding`, and the `model` that
produced it (the vector-space identity, G1.16 — cosine across two models is meaningless).

Indexes: a unique `(document_id, chunk_index, model)` so re-ingesting a document under the same
model is idempotent, and an **HNSW** ANN index on `embedding` (vector_cosine_ops, the same
`m=16, ef_construction=64` as the system tables, R4) — a competent baseline retrieves
efficiently. A k-NN query must order by `<=>` to use it; the retrieval query does. The HNSW
index is created via `op.execute` because alembic has no native HNSW DDL. Both index and table
are mirrored in iknos.db.orm (`BaselineChunk`) so the autogenerate-drift gate stays clean.

Search-path discipline (CI_MIGRATIONS.md §2): although this migration has **no AGE DDL**, a
prior AGE migration (0004/0007/0011) sets `search_path = ag_catalog, "$user", public` for the
session and never resets it (only 0001 does). This is the first relational DDL after them, so an
unqualified `CREATE TABLE` would land in `ag_catalog`/the graph schema, not `public` — leaving
the table (and its HNSW index) invisible to the `public`-search_path app and to the downgrade
drops. So `upgrade()` and `downgrade()` both pin `SET search_path = public` before any relational
statement. The revision id is kept short (`alembic_version.version_num` is varchar(32)).
"""

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_baseline_chunks"
down_revision = "0013_embedding_hnsw_indexes"
branch_labels = None
depends_on = None

_HNSW_INDEX = "ix_baseline_chunks_embedding_hnsw"


def upgrade() -> None:
    # Pin public: a prior AGE migration left search_path = ag_catalog,"$user",public for the
    # session, so an unqualified CREATE TABLE would land in the wrong schema (CI_MIGRATIONS.md §2).
    op.execute("SET search_path = public")
    op.create_table(
        "baseline_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1024), nullable=False),
        sa.Column(
            "model",
            sa.Text(),
            nullable=False,
            comment="Embedding model id — the ANN vector-space identity (G1.16).",
        ),
    )
    op.create_index("ix_baseline_chunks_document_id", "baseline_chunks", ["document_id"])
    op.create_index(
        "uq_baseline_chunks_doc_index_model",
        "baseline_chunks",
        ["document_id", "chunk_index", "model"],
        unique=True,
    )
    # HNSW ANN index — no native alembic DDL for the opclass + WITH params, so emit it raw.
    op.execute(
        f"CREATE INDEX {_HNSW_INDEX} ON baseline_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("SET search_path = public")  # drops must resolve in public (CI_MIGRATIONS.md §2)
    op.execute(f"DROP INDEX {_HNSW_INDEX}")
    op.drop_index("uq_baseline_chunks_doc_index_model", table_name="baseline_chunks")
    op.drop_index("ix_baseline_chunks_document_id", table_name="baseline_chunks")
    op.drop_table("baseline_chunks")
