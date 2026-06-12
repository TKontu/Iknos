"""G1.7b cross-document "extract once" reuse — live Postgres+AGE.

Where ``test_extraction_cache.py`` pins the G1.7 *soundness* invariant (two identical-text spans
both materialize — the key is per-span, not pure-content), this pins the G1.7b *cost* closure built
on top of it: the second span no longer pays the LLM. When a never-extracted span's pipeline
content_hash matches a prior committed extraction, its propositions are **replayed** into the new
span — new nodes, fresh local embeddings, copied faithfulness — skipping only the model call, with a
``reused_from`` audit pointer back to the source.

LLM + embedding substrate mocked (no vLLM / model download); spans are hand-created, as in the
sibling cache/proposition-layer tests.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.proposition import Propositionizer
from iknos.core.verify import Verifier
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher, parse_agtype_map
from iknos.types.epistemic import decode_provisional_reasons
from iknos.types.nodes import Span

pytestmark = pytest.mark.asyncio


def _propositionizer(*, model: str = "test-model", reuse: bool = True) -> Propositionizer:
    llm = MagicMock()
    llm.model = model
    llm.guided_complete = AsyncMock(
        return_value={"propositions": [{"text": "The bearing failed under load."}]}
    )
    substrate = MagicMock()
    substrate.model_name = "BAAI/bge-m3"  # vector-space identity (G1.16)
    substrate.embed_passages = MagicMock(side_effect=lambda texts: [[0.1] * 1024 for _ in texts])
    return Propositionizer(llm, substrate, context_window=8, concurrency=4, reuse_extractions=reuse)


def _attach_verifier(p: Propositionizer) -> Propositionizer:
    """A verifier whose verdict is always fully-preserved entailment → faithfulness 1.0.

    Both the source and the reusing propositionizer must carry the *same* verifier signature for
    their content hashes to match (the signature is in the key) — so the same constructor is used
    for each; only the source ever actually calls it.
    """
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


async def _props_on(session: AsyncSession, span: Span) -> list[str]:
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.text",
        returns="txt agtype",
    )
    return [str(r[0]).strip('"') for r in rows]


async def _extract_action(session: AsyncSession, span: Span) -> tuple[uuid.UUID, dict, dict]:
    row = (
        await session.execute(
            text(
                "SELECT id, inputs, outputs FROM actions "
                "WHERE actor = 'propositionizer' AND inputs->>'target_span' = :sid"
            ),
            {"sid": str(span.id)},
        )
    ).one()
    return row.id, row.inputs, row.outputs


async def _dense_count(session: AsyncSession, doc_id: uuid.UUID) -> int:
    res = await session.execute(
        text("SELECT count(*) FROM proposition_embeddings WHERE document_id = :d"),
        {"d": doc_id},
    )
    return res.scalar_one()


async def test_identical_text_cross_doc_replays_without_llm(session: AsyncSession) -> None:
    """The second document's identical span replays the first's extraction: no LLM call, its own
    propositions + dense rows materialize, and the extract Action points back to the source."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_a, span_a = await _seed_one_span_doc(session, raw)
    doc_b, span_b = await _seed_one_span_doc(session, raw)  # identical text, different document

    await _propositionizer().propositionize_document(session, doc_a, [span_a], raw)
    a_action_id, _, _ = await _extract_action(session, span_a)

    reuser = _propositionizer()
    report = await reuser.propositionize_document(session, doc_b, [span_b], raw)

    # The reusing run never called the extractor LLM — the whole point of G1.7b.
    assert reuser.llm.guided_complete.await_count == 0
    assert len(report.action_ids) == 1
    assert report.failed_spans == []

    # span_b still materialized its own propositions (the soundness invariant) + dense rows.
    assert await _props_on(session, span_b) == ["The bearing failed under load."]
    assert await _dense_count(session, doc_b) == 1

    # The replay extract Action carries the reused_from pointer to the source span + Action.
    _, b_inputs, b_outputs = await _extract_action(session, span_b)
    assert b_inputs["reused_from"] == {"span": str(span_a.id), "action": str(a_action_id)}
    assert len(b_outputs["propositions"]) == 1

    # And the per-span idempotency key is intact: a third run of span_b is a true no-op.
    again = await _propositionizer().propositionize_document(session, doc_b, [span_b], raw)
    assert again.action_ids == []
    assert await _dense_count(session, doc_b) == 1  # no duplicate dense rows


async def test_reuse_disabled_falls_back_to_llm(session: AsyncSession) -> None:
    """With reuse off, an identical second span re-extracts via the LLM (no reused_from) — the
    flag is the deploy-time escape hatch back to always re-extracting."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_a, span_a = await _seed_one_span_doc(session, raw)
    doc_b, span_b = await _seed_one_span_doc(session, raw)

    await _propositionizer().propositionize_document(session, doc_a, [span_a], raw)

    reuser_off = _propositionizer(reuse=False)
    await reuser_off.propositionize_document(session, doc_b, [span_b], raw)

    assert reuser_off.llm.guided_complete.await_count == 1  # the LLM was called
    _, b_inputs, _ = await _extract_action(session, span_b)
    assert "reused_from" not in b_inputs


async def test_reused_faithfulness_is_copied_not_reverified(session: AsyncSession) -> None:
    """The reused faithfulness/provisional are copied from the source nodes; the reusing run does
    not re-call its verifier and records no verify Action (the source's verdict is one hop away)."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_a, span_a = await _seed_one_span_doc(session, raw)
    doc_b, span_b = await _seed_one_span_doc(session, raw)

    await _attach_verifier(_propositionizer()).propositionize_document(
        session, doc_a, [span_a], raw
    )

    reuser = _attach_verifier(_propositionizer())
    await reuser.propositionize_document(session, doc_b, [span_b], raw)

    # The reusing verifier was never called — faithfulness came from the cached node.
    assert reuser.verifier is not None
    assert reuser.verifier.llm.guided_complete.await_count == 0

    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span_b.id)})}) "
        "RETURN p.faithfulness, p.provisional, properties(p)",
        returns="faith agtype, prov agtype, props agtype",
    )
    assert float(str(rows[0][0]).strip('"')) == pytest.approx(1.0)
    assert str(rows[0][1]).strip('"').lower() == "false"
    # R8: the reason set round-trips through replay too (copied from the cached node).
    assert decode_provisional_reasons(parse_agtype_map(rows[0][2]).get("provisional_reasons")) == []

    # No verify Action for the replayed span — the reused faithfulness is audited at the source.
    no_verify = await session.execute(
        text(
            "SELECT count(*) FROM actions "
            "WHERE actor = 'verifier' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span_b.id)},
    )
    assert no_verify.scalar_one() == 0


async def test_empty_extraction_replays_without_llm(session: AsyncSession) -> None:
    """A cached *empty* extraction (the source span asserted no factual claim) is itself reusable:
    the second span replays zero propositions, skips the LLM, and records an extract Action so its
    own next run no-ops."""
    await bootstrap_session(session)
    raw = "Hello there."
    doc_a, span_a = await _seed_one_span_doc(session, raw)
    doc_b, span_b = await _seed_one_span_doc(session, raw)

    empty = _propositionizer()
    empty.llm.guided_complete = AsyncMock(return_value={"propositions": []})
    await empty.propositionize_document(session, doc_a, [span_a], raw)
    a_action_id, _, _ = await _extract_action(session, span_a)

    reuser = _propositionizer()
    await reuser.propositionize_document(session, doc_b, [span_b], raw)

    assert reuser.llm.guided_complete.await_count == 0  # skipped even for the empty result
    assert await _props_on(session, span_b) == []
    _, b_inputs, b_outputs = await _extract_action(session, span_b)
    assert b_inputs["reused_from"] == {"span": str(span_a.id), "action": str(a_action_id)}
    assert b_outputs["propositions"] == []

    # The empty replay still recorded the content_hash → span_b's next run is a no-op.
    again = await _propositionizer().propositionize_document(session, doc_b, [span_b], raw)
    assert again.action_ids == []
