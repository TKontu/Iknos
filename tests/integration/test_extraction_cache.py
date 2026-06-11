"""G1.7 content-addressed, version-aware extraction idempotency + G1.7r cascade re-extraction —
live Postgres+AGE.

Complements the no-op idempotency assertion in ``test_proposition_layer.py`` with the cases the
versioned key adds: (1) a span re-run under a *changed* pipeline (model / verifier) is **cascade
re-extracted** — its superseded propositions + their dense/lexical index rows purged, the new ones
written, the swap audited — instead of silently serving the stale extraction or orphaning rows
(G1.7r); (1b) with cascade **disabled** the same change fails loud with no partial writes (the
conservative G1.7 mode); (1c) a stale span whose propositions already feed downstream nodes is
**refused** (``CascadeDependentsError``) rather than orphaning them; (2) two different spans with
*identical* text both materialize — the soundness guard that the key is per-span, not pure content.

LLM + embedding substrate mocked (no vLLM / model download); spans are hand-created, as in the
sibling proposition-layer test.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.proposition import (
    CascadeDependentsError,
    Propositionizer,
    StaleExtractionError,
)
from iknos.core.verify import Verifier
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher, unquote_agtype
from iknos.types.nodes import Span

pytestmark = pytest.mark.asyncio


def _propositionizer(model: str = "test-model", **kw: object) -> Propositionizer:
    llm = MagicMock()
    llm.model = model
    llm.guided_complete = AsyncMock(
        return_value={"propositions": [{"text": "The bearing failed under load."}]}
    )
    substrate = MagicMock()
    substrate.model_name = "BAAI/bge-m3"  # vector-space identity (G1.16)
    substrate.embed_passages = MagicMock(return_value=[[0.1] * 1024])
    return Propositionizer(llm, substrate, context_window=8, concurrency=4, **kw)  # type: ignore[arg-type]


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


async def _prop_ids(session: AsyncSession, span: Span) -> set[str]:
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.id",
        returns="pid agtype",
    )
    return {unquote_agtype(r[0]) for r in rows}


async def _prop_count(session: AsyncSession, span: Span) -> int:
    return len(await _prop_ids(session, span))


async def _index_counts(session: AsyncSession, doc_id: uuid.UUID) -> tuple[int, int]:
    """``(proposition_embeddings, proposition_lexical_index)`` row counts for a document — the
    stores a cascade purge must keep consistent with the graph (no orphaned index rows)."""
    emb = await session.execute(
        text("SELECT count(*) FROM proposition_embeddings WHERE document_id = :d"), {"d": doc_id}
    )
    lex = await session.execute(
        text("SELECT count(*) FROM proposition_lexical_index WHERE document_id = :d"), {"d": doc_id}
    )
    return emb.scalar_one(), lex.scalar_one()


async def _extract_action_count(session: AsyncSession, span: Span) -> int:
    res = await session.execute(
        text(
            "SELECT count(*) FROM actions "
            "WHERE actor = 'propositionizer' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span.id)},
    )
    return res.scalar_one()


async def test_changed_model_cascade_reextracts_and_purges(session: AsyncSession) -> None:
    """A span re-run under a different extractor model is **cascade re-extracted** (G1.7r): the old
    proposition + its dense/lexical index rows are purged and the new one written — no duplicate,
    no orphaned index rows — and the swap is recorded with a ``superseded`` audit pointer."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    await _propositionizer(model="extractor-v1").propositionize_document(
        session, doc_id, [span], raw
    )
    [old_id] = list(await _prop_ids(session, span))
    assert await _index_counts(session, doc_id) == (1, 1)

    upgraded = _propositionizer(model="extractor-v2")
    await upgraded.propositionize_document(session, doc_id, [span], raw)

    # The pipeline changed, so the LLM ran again (not a no-op) — and the old proposition is gone.
    assert upgraded.llm.guided_complete.await_count == 1
    new_ids = await _prop_ids(session, span)
    assert len(new_ids) == 1  # exactly one proposition — purged-then-rewritten, never duplicated
    assert old_id not in new_ids  # the superseded proposition was deleted
    # No orphaned index rows: the old embedding + lexical rows were purged with the vertex.
    assert await _index_counts(session, doc_id) == (1, 1)
    # Two extract Actions (append-only audit); the second records what it superseded.
    assert await _extract_action_count(session, span) == 2
    res = await session.execute(
        text(
            "SELECT outputs->'superseded' FROM actions WHERE actor = 'propositionizer' "
            "AND inputs->>'target_span' = :sid AND outputs ? 'superseded'"
        ),
        {"sid": str(span.id)},
    )
    superseded = res.scalar_one()
    assert superseded == [old_id]


async def test_cascade_disabled_fails_loud_no_writes(session: AsyncSession) -> None:
    """With ``cascade_reextract=False`` the conservative G1.7 mode stands: a changed pipeline raises
    ``StaleExtractionError`` before any inference or write, rather than overwriting."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    await _propositionizer(model="extractor-v1").propositionize_document(
        session, doc_id, [span], raw
    )
    [old_id] = list(await _prop_ids(session, span))

    upgraded = _propositionizer(model="extractor-v2", cascade_reextract=False)
    with pytest.raises(StaleExtractionError):
        await upgraded.propositionize_document(session, doc_id, [span], raw)

    # Failed loud, before inference and before any write — the original extraction is untouched.
    assert upgraded.llm.guided_complete.await_count == 0
    assert await _prop_ids(session, span) == {old_id}
    assert await _index_counts(session, doc_id) == (1, 1)
    assert await _extract_action_count(session, span) == 1


async def test_toggling_verifier_cascade_reextracts(session: AsyncSession) -> None:
    """The verifier signature is in the key, so enabling the verifier on an already-extracted span
    invalidates it (its faithfulness would otherwise never be computed) — and cascade re-extracts
    it, now with a verify Action behind the new proposition's faithfulness."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    await _propositionizer().propositionize_document(session, doc_id, [span], raw)
    [old_id] = list(await _prop_ids(session, span))

    await _attach_verifier(_propositionizer()).propositionize_document(session, doc_id, [span], raw)

    new_ids = await _prop_ids(session, span)
    assert len(new_ids) == 1 and old_id not in new_ids  # re-extracted, old purged
    assert await _index_counts(session, doc_id) == (1, 1)  # no orphaned index rows
    # The re-extraction recorded a verify Action this time (faithfulness now has a verdict).
    verify_actions = await session.execute(
        text(
            "SELECT count(*) FROM actions WHERE actor = 'verifier' "
            "AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span.id)},
    )
    assert verify_actions.scalar_one() == 1


async def test_cascade_refuses_when_propositions_have_dependents(session: AsyncSession) -> None:
    """A stale span whose propositions already feed a downstream node is **refused**
    (``CascadeDependentsError``) — purging would orphan the consumer, so the full downstream cascade
    stays deferred. Nothing is purged."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    await _propositionizer(model="extractor-v1").propositionize_document(
        session, doc_id, [span], raw
    )
    [old_id] = list(await _prop_ids(session, span))

    # Simulate a Phase-2 consumer: a Fact evidenced by this proposition (its 2nd edge).
    await execute_cypher(
        session,
        f"MATCH (p:Proposition {cypher_map({'id': old_id})}) "
        f"CREATE (:Fact {cypher_map({'id': str(uuid.uuid4())})})-[:EVIDENCED_BY]->(p)",
    )
    await session.commit()

    upgraded = _propositionizer(model="extractor-v2")
    with pytest.raises(CascadeDependentsError):
        await upgraded.propositionize_document(session, doc_id, [span], raw)

    # Refused before inference and before any purge — the original proposition survives.
    assert upgraded.llm.guided_complete.await_count == 0
    assert await _prop_ids(session, span) == {old_id}
    assert await _index_counts(session, doc_id) == (1, 1)


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
