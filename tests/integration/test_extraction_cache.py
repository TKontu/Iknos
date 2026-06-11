"""G1.7 content-addressed, version-aware extraction idempotency — live Postgres+AGE.

Complements the no-op idempotency assertion in ``test_proposition_layer.py`` with the two cases
G1.7 adds: (1) a span re-run under a *changed* pipeline (model / verifier) fails loud instead of
silently serving the stale extraction, with no partial writes; (2) two different spans carrying
*identical* text both materialize — the soundness guard that the key is per-span, not purely
content (a pure-content skip would drop the second span's propositions).

LLM + embedding substrate mocked (no vLLM / model download); spans are hand-created, as in the
sibling proposition-layer test.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.proposition import Propositionizer, StaleExtractionError
from iknos.core.verify import Verifier
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.types.nodes import Span

pytestmark = pytest.mark.asyncio


def _propositionizer(model: str = "test-model") -> Propositionizer:
    llm = MagicMock()
    llm.model = model
    llm.guided_complete = AsyncMock(
        return_value={"propositions": [{"text": "The bearing failed under load."}]}
    )
    substrate = MagicMock()
    substrate.model_name = "BAAI/bge-m3"  # vector-space identity (G1.16)
    substrate.embed_passages = MagicMock(return_value=[[0.1] * 1024])
    return Propositionizer(llm, substrate, context_window=8, concurrency=4)


def _attach_verifier(p: Propositionizer) -> Propositionizer:
    vllm = MagicMock()
    vllm.model = "verifier-model"
    vllm.guided_complete = AsyncMock(
        return_value={
            "verdicts": [
                {
                    "entailment": "entailed",
                    "polarity_preserved": True,
                    "modality_preserved": True,
                    "attribution_preserved": True,
                }
            ]
        }
    )
    p.verifier = Verifier(vllm)
    return p


async def _seed_one_span_doc(session: AsyncSession, raw: str) -> tuple[uuid.UUID, Span]:
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": raw},
    )
    await execute_cypher(session, f"CREATE (:Document {cypher_map({'id': str(doc_id)})})")
    span = Span(id=uuid.uuid4(), document_id=doc_id, start=0, end=len(raw))
    await execute_cypher(
        session,
        "CREATE (:Span "
        + cypher_map(
            {"id": str(span.id), "document_id": str(doc_id), "start": span.start, "end": span.end}
        )
        + ")",
    )
    await session.commit()
    return doc_id, span


async def _prop_count(session: AsyncSession, span: Span) -> int:
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p",
        returns="p agtype",
    )
    return len(rows)


async def _extract_action_count(session: AsyncSession, span: Span) -> int:
    res = await session.execute(
        text(
            "SELECT count(*) FROM actions "
            "WHERE actor = 'propositionizer' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span.id)},
    )
    return res.scalar_one()


async def test_changed_model_raises_stale_with_no_partial_writes(session: AsyncSession) -> None:
    """Re-running a span under a different extractor model raises rather than silently skipping
    (the old span-id-only check) or duplicating — and writes nothing in the process."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    await _propositionizer(model="extractor-v1").propositionize_document(
        session, doc_id, [span], raw
    )
    assert await _prop_count(session, span) == 1
    assert await _extract_action_count(session, span) == 1

    upgraded = _propositionizer(model="extractor-v2")
    with pytest.raises(StaleExtractionError):
        await upgraded.propositionize_document(session, doc_id, [span], raw)

    # Failed loud, before inference and before any write.
    assert upgraded.llm.guided_complete.await_count == 0
    assert await _prop_count(session, span) == 1  # no duplicate propositions
    assert await _extract_action_count(session, span) == 1  # no second extract Action


async def test_toggling_verifier_raises_stale(session: AsyncSession) -> None:
    """The verifier signature is in the key: enabling the verifier on an already-extracted span
    invalidates it (its faithfulness would otherwise never be computed)."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    await _propositionizer().propositionize_document(session, doc_id, [span], raw)

    with pytest.raises(StaleExtractionError):
        await _attach_verifier(_propositionizer()).propositionize_document(
            session, doc_id, [span], raw
        )


async def test_identical_text_different_span_both_materialize(session: AsyncSession) -> None:
    """Soundness: the key is (span_id, content_hash), not content alone. Two spans with identical
    text — hence an identical content_hash — must each get their own propositions; a pure-content
    skip would drop the second span entirely."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_a, span_a = await _seed_one_span_doc(session, raw)
    doc_b, span_b = await _seed_one_span_doc(session, raw)  # identical text, different document

    await _propositionizer().propositionize_document(session, doc_a, [span_a], raw)
    await _propositionizer().propositionize_document(session, doc_b, [span_b], raw)

    assert await _prop_count(session, span_a) == 1
    assert await _prop_count(session, span_b) == 1  # not skipped despite the colliding content

    # The content hashes really are equal (same target text, empty context, same model) — so the
    # only thing that kept span_b from being skipped is the per-span keying.
    hashes = await session.execute(
        text(
            "SELECT inputs->>'target_span', inputs->>'content_hash' FROM actions "
            "WHERE actor = 'propositionizer' AND inputs->>'target_span' = ANY(:ids)"
        ),
        {"ids": [str(span_a.id), str(span_b.id)]},
    )
    by_span = {row[0]: row[1] for row in hashes}
    assert by_span[str(span_a.id)] == by_span[str(span_b.id)]
