"""hnsw ANN indexes on both pgvector embedding tables + cosine standardization (R4)

Revision ID: 0013_embedding_hnsw_indexes
Revises: 0012_actions_content_hash_index
Create Date: 2026-06-12

Touches AGE graph: no (relational only)

`document_embeddings` and `proposition_embeddings` carry the dense index (§4) the candidate
funnel's embedding k-NN stage searches (§5.1, `core/candidates.py`), but **neither has an ANN
index** — every nearest-neighbour query is a sequential scan + full sort over the table. That is
fine at today's tiny scale and is the documented recall ceiling the in-memory exact path measures
against (G4.2 slice 2), but it is the scale cliff the gate's recall measurement (V9) and any real
corpus hit. This adds an **HNSW** index on the `embedding` column of both tables.

**Cosine, standardized (`<=>`).** The vectors are L2-normalized (bge-m3), so cosine distance and
inner product rank identically; cosine (`vector_cosine_ops`, the `<=>` operator) is chosen as the
one standard for robustness if normalization ever drifts. A k-NN query **must** order by `<=>`
against this opclass or the planner cannot use the index — the `embedding` ORM columns carry that
note, and the V9 push-down query will match the opclass.

`m = 16, ef_construction = 64` are pgvector's defaults — a sound starting point; build-time vs
recall tuning is a later, data-driven call (the gate corpus), not guessed here. Created via
`op.execute` because alembic has no native HNSW DDL (the opclass + `WITH` params do not render
through `op.create_index`). Mirrored in `iknos.db.orm` (`DocumentEmbedding`/`PropositionEmbedding`
`__table_args__`) via pgvector-sqlalchemy's `postgresql_using='hnsw'` so the autogenerate-drift
gate stays clean. Downgrade drops both. The revision id is kept short
(`alembic_version.version_num` is `varchar(32)`).
"""

from alembic import op

revision = "0013_embedding_hnsw_indexes"
down_revision = "0012_actions_content_hash_index"
branch_labels = None
depends_on = None

# (index name, table) — the HNSW ANN index on each table's `embedding` column.
_INDEXES = (
    ("ix_document_embeddings_embedding_hnsw", "document_embeddings"),
    ("ix_proposition_embeddings_embedding_hnsw", "proposition_embeddings"),
)


def upgrade() -> None:
    for name, table in _INDEXES:
        op.execute(
            f"CREATE INDEX {name} ON {table} "
            "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )


def downgrade() -> None:
    for name, _table in _INDEXES:
        op.execute(f"DROP INDEX {name}")
