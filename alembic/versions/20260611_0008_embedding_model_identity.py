"""embedding model identity: record the vector space on every dense row (G1.16)

Revision ID: 0008_embedding_model_identity
Revises: 0007_age_label_indexes
Create Date: 2026-06-11

Touches AGE graph: no (relational only)

Closes docs/gap_phase_1_ingest.md G1.16 (2026-06 review A5). `document_embeddings` and
`proposition_embeddings` rows carried no record of *which* model produced them. Swap or
upgrade the embedding model and the pgvector ANN index becomes a mixed-space soup — cosine
across two embedding spaces is meaningless — and nothing could even detect the condition.
Dimension is implicit in the pgvector column, so a model change that alters dimension already
fails loud; the silent case this closes is a **same-dimension** swap (e.g. one 1024-d model
for another).

Adds `model TEXT NOT NULL` to both tables. The column is added with a server_default of
'BAAI/bge-m3' (the only model in use to date) so existing rows backfill atomically to the
correct value; the default is then dropped so the application must specify the model on every
insert going forward — a future model is never silently defaulted into the wrong vector space.
A column comment records that `model` is the vector-space identifier.

The ingest guard (core/embeddings.py::EmbeddingModelMismatchError, raised in
core/ingest.py::persist_spans and core/proposition.py) and the reindex path
(scripts/reembed.py) consume this column. Mirrored in iknos.db.orm
(DocumentEmbedding.model / PropositionEmbedding.model) so the autogenerate-drift gate
stays clean.
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_embedding_model_identity"
down_revision = "0007_age_label_indexes"
branch_labels = None
depends_on = None

# The sole embedding model in use before this migration; existing rows are this space.
_BACKFILL_MODEL = "BAAI/bge-m3"
_TABLES = ("document_embeddings", "proposition_embeddings")


def upgrade() -> None:
    for table in _TABLES:
        # NOT NULL + server_default backfills every existing row in one statement...
        op.add_column(
            table,
            sa.Column(
                "model",
                sa.Text(),
                nullable=False,
                server_default=_BACKFILL_MODEL,
                comment="Embedding model id — the ANN vector-space identity (G1.16).",
            ),
        )
        # ...then drop the default so future inserts MUST name their model explicitly (the app
        # always knows it: substrate.model_name). A silent default is exactly the mixing this
        # guards against.
        op.alter_column(table, "model", server_default=None)


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "model")
