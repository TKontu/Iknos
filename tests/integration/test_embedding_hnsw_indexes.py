"""R4 — HNSW ANN indexes on the pgvector embedding tables, on a migrated DB.

Proves migration 0013 is not just applied but *effective*: both `embedding` columns carry an HNSW
index, and the planner actually uses it for a cosine (`<=>`) k-NN — the operator the V9 push-down
and the gate recall measurement depend on. `SET enable_seqscan = off` because the test tables are
tiny (a seq scan would otherwise win on cost); the point is index *eligibility* via the
`vector_cosine_ops` opclass, which a wrong-operator query would forfeit.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

# (table, hnsw index name) — both pgvector dense indexes migration 0013 covers.
_TABLES = (
    ("document_embeddings", "ix_document_embeddings_embedding_hnsw"),
    ("proposition_embeddings", "ix_proposition_embeddings_embedding_hnsw"),
)

# A literal unit vector of the fixed 1024 dimension (§4 / G1.16) to order by.
_VEC = "[" + ",".join(["0.1"] * 1024) + "]"


@pytest.mark.parametrize(("table", "index"), _TABLES)
async def test_hnsw_index_exists(session: AsyncSession, table: str, index: str) -> None:
    """Migration 0013 created the HNSW index with the cosine opclass on each table."""
    row = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :i",
        ),
        {"t": table, "i": index},
    )
    indexdef = row.scalar_one_or_none()
    assert indexdef is not None, f"{index} missing on {table} — migration 0013 not applied"
    assert "USING hnsw" in indexdef
    assert "vector_cosine_ops" in indexdef  # the `<=>` opclass the k-NN must match


@pytest.mark.parametrize(("table", "index"), _TABLES)
async def test_cosine_knn_uses_the_hnsw_index(
    session: AsyncSession, table: str, index: str
) -> None:
    """The planner uses the HNSW index for a `<=>` (cosine) ordered k-NN — the query shape the V9
    push-down (and the in-memory exact path's recall oracle) will issue."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))  # tiny table — force the choice
    plan = await session.execute(
        text(f"EXPLAIN SELECT id FROM {table} ORDER BY embedding <=> '{_VEC}' LIMIT 10")
    )
    plan_text = "\n".join(r[0] for r in plan)
    assert index in plan_text, f"planner did not use {index}:\n{plan_text}"
