"""Phase 1 Increment 3 integration test — proposition layer end to end.

Exercises real Postgres+AGE persistence with the LLM and embedding substrate
mocked (no vLLM or model download needed). Span vertices are created by the test
itself: materializing spans into AGE is a separate follow-up, so this increment
assumes they already exist.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.proposition import Propositionizer
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.db.spans import resolve_span_text
from iknos.types.nodes import Span

pytestmark = pytest.mark.asyncio


def _mock_propositionizer(llm_return: dict, n_vectors: int) -> Propositionizer:
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(return_value=llm_return)
    substrate = MagicMock()
    substrate.embed_passages = MagicMock(
        return_value=[[0.1 * (i + 1)] * 1024 for i in range(n_vectors)]
    )
    return Propositionizer(llm, substrate, context_window=8, concurrency=4)


async def test_proposition_layer_end_to_end(session: AsyncSession) -> None:
    await bootstrap_session(session)

    doc_id = uuid.uuid4()
    raw = "Smith reviewed the report. He argued the AB-1234 flood-defense budget was insufficient."
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :text)"),
        {"id": doc_id, "text": raw},
    )
    await execute_cypher(session, f"CREATE (:Document {cypher_map({'id': str(doc_id)})})")

    # Two spans; the target is the second sentence (resolves "He" -> Smith via context).
    ctx_start, ctx_end = 0, 26
    tgt_start, tgt_end = 27, len(raw)
    ctx_span = Span(id=uuid.uuid4(), document_id=doc_id, start=ctx_start, end=ctx_end)
    tgt_span = Span(id=uuid.uuid4(), document_id=doc_id, start=tgt_start, end=tgt_end)
    for s in (ctx_span, tgt_span):
        await execute_cypher(
            session,
            "CREATE (:Span "
            + cypher_map(
                {"id": str(s.id), "document_id": str(doc_id), "start": s.start, "end": s.end}
            )
            + ")",
        )
    await session.commit()

    p = _mock_propositionizer(
        llm_return={
            "propositions": [
                {"text": "Smith argued the AB-1234 flood-defense budget was insufficient."},
                {"text": "Smith reviewed the report."},
            ]
        },
        n_vectors=2,
    )

    action_ids = await p.propositionize_document(session, doc_id, [ctx_span, tgt_span], raw)
    assert len(action_ids) == 2  # one Action per span (the context span yields the same mock)

    # --- Propositions are walkable: Proposition -> EVIDENCED_BY -> target Span -> source text ---
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(s:Span {cypher_map({'id': str(tgt_span.id)})}) "
        "RETURN p",
        returns="p agtype",
    )
    assert len(rows) == 2
    assert await resolve_span_text(session, doc_id, tgt_start, tgt_end) == raw[tgt_start:tgt_end]

    # --- Dense rows: one per proposition, 1024-dim ---
    dense = await session.execute(
        text("SELECT count(*) FROM proposition_embeddings WHERE document_id = :d"),
        {"d": doc_id},
    )
    assert dense.scalar_one() == 4  # 2 props for target + 2 for context span

    # --- Sparse lexical-exact: the AB-1234 code is recoverable (simple config, unstemmed) ---
    lex = await session.execute(
        text(
            "SELECT count(*) FROM proposition_lexical_index "
            "WHERE document_id = :d AND lexemes @@ plainto_tsquery('simple', 'AB-1234')"
        ),
        {"d": doc_id},
    )
    assert lex.scalar_one() >= 1

    # --- Action: joinable to its propositions by output id (point auditability, §10.2) ---
    act = await session.execute(
        text(
            "SELECT action_type, model, inputs, outputs FROM actions "
            "WHERE inputs->>'target_span' = :sid"
        ),
        {"sid": str(tgt_span.id)},
    )
    rec = act.one()
    assert rec.action_type == "extract"
    assert rec.model == "test-model"
    assert str(ctx_span.id) in rec.inputs["context_spans"]
    assert len(rec.outputs["propositions"]) == 2

    # --- Idempotency: a second run is a no-op (Action-based skip) ---
    llm_before = p.llm.guided_complete.await_count
    again = await p.propositionize_document(session, doc_id, [ctx_span, tgt_span], raw)
    assert again == []
    assert p.llm.guided_complete.await_count == llm_before  # no new inference
    dense_after = await session.execute(
        text("SELECT count(*) FROM proposition_embeddings WHERE document_id = :d"),
        {"d": doc_id},
    )
    assert dense_after.scalar_one() == 4
