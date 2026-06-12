"""Phase 1 Increment 3 integration test — proposition layer end to end.

Exercises real Postgres+AGE persistence with the LLM and embedding substrate
mocked (no vLLM or model download needed). Span vertices are created by the test
itself: materializing spans into AGE is a separate follow-up, so this increment
assumes they already exist.
"""

import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.proposition import Propositionizer
from iknos.core.verify import Verifier
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher, parse_agtype_map
from iknos.db.spans import resolve_span_text
from iknos.types.epistemic import decode_provisional_reasons
from iknos.types.nodes import Span

pytestmark = pytest.mark.asyncio


def _mock_propositionizer(llm_return: dict, n_vectors: int) -> Propositionizer:
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(return_value=llm_return)
    substrate = MagicMock()
    substrate.model_name = "BAAI/bge-m3"  # vector-space identity written on each row (G1.16)
    substrate.embed_passages = MagicMock(
        return_value=[[0.1 * (i + 1)] * 1024 for i in range(n_vectors)]
    )
    return Propositionizer(llm, substrate, context_window=8, concurrency=4)


# 1024-dim toy vectors for clustering: A ⟂ B (cosine 0), so candidates with the same letter
# cluster and different letters split — independent of the real embedding model.
_VEC_A = [1.0] + [0.0] * 1023
_VEC_B = [0.0, 1.0] + [0.0] * 1022


def _multi_sample_propositionizer(
    side_effect: list[dict], embed_return: list[list[float]]
) -> Propositionizer:
    """A Propositionizer that returns a different extraction per sample (G1.3). n_samples is the
    number of side_effect entries; sampling is non-greedy so the multi-sample guard is satisfied."""
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(side_effect=side_effect)
    substrate = MagicMock()
    substrate.model_name = "BAAI/bge-m3"  # vector-space identity (G1.16)
    substrate.embed_passages = MagicMock(return_value=embed_return)
    return Propositionizer(
        llm,
        substrate,
        context_window=8,
        concurrency=4,
        sampling={"temperature": 0.7},
        n_samples=len(side_effect),
    )


def _with_verifier(p: Propositionizer, verdict_for: Callable[[str], dict]) -> Propositionizer:
    """Attach a real Verifier whose LLM returns a verdict chosen from the proposition text.

    Uses the real Verifier (build_messages + faithfulness derivation) with only the LLM
    endpoint mocked — so the persisted faithfulness/provisional are computed end-to-end.
    """
    vllm = MagicMock()
    vllm.model = "verifier-model"

    async def _guided(messages: list[dict], schema: dict, sampling: dict | None = None) -> dict:
        return {"verdicts": [verdict_for(messages[1]["content"])]}

    vllm.guided_complete = AsyncMock(side_effect=_guided)
    p.verifier = Verifier(vllm)
    return p


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

    report = await p.propositionize_document(session, doc_id, [ctx_span, tgt_span], raw)
    assert len(report.action_ids) == 2  # one Action per span (context span yields the same mock)
    assert report.failed_spans == []

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
    assert again.action_ids == []
    assert again.failed_spans == []
    assert p.llm.guided_complete.await_count == llm_before  # no new inference
    dense_after = await session.execute(
        text("SELECT count(*) FROM proposition_embeddings WHERE document_id = :d"),
        {"d": doc_id},
    )
    assert dense_after.scalar_one() == 4


async def test_epistemic_fields_and_routing_persist(session: AsyncSession) -> None:
    """G1.1/G1.2: epistemic fields land on the Proposition vertex; routing is derived
    from epistemic_class; faithfulness/provisional are null (owned by G1.4/G1.5/G1.6)."""
    await bootstrap_session(session)

    doc_id = uuid.uuid4()
    raw = "The supplier reported an assembly fault. The surface shows indentations."
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

    p = _mock_propositionizer(
        llm_return={
            "propositions": [
                {
                    "text": "The failure was an assembly fault.",
                    "polarity": "asserted",
                    "modality": "probable",
                    "attribution": "named-source",
                    "scope": "",
                    "epistemic_class": "judgement",
                },
                {
                    "text": "The rolling surface shows particle indentations.",
                    "polarity": "asserted",
                    "modality": "categorical",
                    "attribution": "document",
                    "scope": "",
                    "epistemic_class": "observation",
                },
            ]
        },
        n_vectors=2,
    )
    await p.propositionize_document(session, doc_id, [span], raw)

    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.text, p.epistemic_class, p.routing, p.modality, p.attribution, "
        "p.faithfulness, p.provisional",
        returns=(
            "text agtype, ec agtype, routing agtype, modality agtype, "
            "attribution agtype, faith agtype, prov agtype"
        ),
    )
    by_text = {str(r[0]).strip('"'): r for r in rows}
    assert len(by_text) == 2

    # A source's conclusion → judgement class → routes to JUDGEMENT (G1.2), never a fact.
    judgement = by_text["The failure was an assembly fault."]
    assert str(judgement[1]).strip('"') == "judgement"
    assert str(judgement[2]).strip('"') == "judgement"
    assert str(judgement[3]).strip('"') == "probable"
    assert str(judgement[4]).strip('"') == "named-source"
    # Faithfulness is not self-reported — null until a verifier computes it. This run has no
    # verifier, so G1.21 makes that null grounding provisional (UNASSESSED_FAITHFULNESS → legacy
    # boolean true); faithfulness itself stays null (never fabricated).
    assert judgement[5] is None
    assert str(judgement[6]).strip('"').lower() == "true"

    # An observation → routes to FACT.
    observation = by_text["The rolling surface shows particle indentations."]
    assert str(observation[1]).strip('"') == "observation"
    assert str(observation[2]).strip('"') == "fact"
    assert observation[5] is None
    assert str(observation[6]).strip('"').lower() == "true"


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


async def test_faithfulness_and_provisional_persist_with_verifier(session: AsyncSession) -> None:
    """G1.4/G1.5: the verifier's verdict drives faithfulness; a dropped negation falls below
    the threshold and is marked provisional, while a fully-preserved entailment is not."""
    await bootstrap_session(session)
    raw = "The surface shows indentations. The bearing did not fail."
    doc_id, span = await _seed_one_span_doc(session, raw)

    p = _mock_propositionizer(
        llm_return={
            "propositions": [
                {"text": "The surface shows indentations."},
                {"text": "The bearing failed.", "polarity": "asserted"},  # dropped the negation
            ]
        },
        n_vectors=2,
    )

    def verdict_for(user: str) -> dict:
        proposition = user.split("PROPOSITION:")[1]  # key on the proposition, not the source span
        if "bearing" in proposition:
            # The source denies it: the extractor dropped the negation → polarity not preserved.
            return {
                "entailment": "entailed",
                "polarity_preserved": False,
                "modality_preserved": True,
                "attribution_preserved": True,
            }
        return {
            "entailment": "entailed",
            "polarity_preserved": True,
            "modality_preserved": True,
            "attribution_preserved": True,
        }

    _with_verifier(p, verdict_for)
    await p.propositionize_document(session, doc_id, [span], raw)

    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.text, p.faithfulness, p.provisional, properties(p)",
        returns="text agtype, faith agtype, prov agtype, props agtype",
    )
    by_text = {str(r[0]).strip('"'): r for r in rows}

    faithful = by_text["The surface shows indentations."]
    assert float(str(faithful[1]).strip('"')) == pytest.approx(1.0)
    # R8: the reason set is the source of truth (empty here); the legacy boolean mirrors it.
    assert (
        decode_provisional_reasons(parse_agtype_map(faithful[3]).get("provisional_reasons")) == []
    )
    assert str(faithful[2]).strip('"').lower() == "false"

    quarantined = by_text["The bearing failed."]
    assert float(str(quarantined[1]).strip('"')) == pytest.approx(0.40)
    assert decode_provisional_reasons(
        parse_agtype_map(quarantined[3]).get("provisional_reasons")
    ) == ["low_faithfulness"]
    assert str(quarantined[2]).strip('"').lower() == "true"


async def test_verify_action_is_recorded(session: AsyncSession) -> None:
    """The verify pass is its own auditable Action (actor=verifier), attributed to the
    verifier model, with decomposed verdicts joinable to the propositions (Trial A5)."""
    await bootstrap_session(session)
    raw = "The surface shows indentations."
    doc_id, span = await _seed_one_span_doc(session, raw)

    p = _mock_propositionizer(
        llm_return={"propositions": [{"text": "The surface shows indentations."}]},
        n_vectors=1,
    )
    _with_verifier(
        p,
        lambda _user: {
            "entailment": "entailed",
            "polarity_preserved": True,
            "modality_preserved": True,
            "attribution_preserved": True,
        },
    )
    await p.propositionize_document(session, doc_id, [span], raw)

    act = await session.execute(
        text(
            "SELECT model, outputs FROM actions "
            "WHERE actor = 'verifier' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span.id)},
    )
    rec = act.one()
    assert rec.model == "verifier-model"
    verdicts = rec.outputs["verdicts"]
    assert len(verdicts) == 1
    assert verdicts[0]["entailment"] == "entailed"
    assert verdicts[0]["faithfulness"] == pytest.approx(1.0)
    assert verdicts[0]["provisional_reasons"] == []

    # The verdict's proposition id is one of the extract Action's outputs (point auditability).
    ext = await session.execute(
        text(
            "SELECT outputs FROM actions "
            "WHERE actor = 'propositionizer' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span.id)},
    )
    assert verdicts[0]["proposition"] in ext.scalar_one()["propositions"]


async def test_multi_sample_agreement_combines_into_faithfulness(session: AsyncSession) -> None:
    """G1.3: with N samples, a proposition produced in every sample is stable (agreement 1.0,
    faithfulness == verify component); one produced in a single sample is unstable (agreement 1/3),
    and the multiplicative combine drops its faithfulness below the threshold → provisional. The
    extract Action audits the regime (n_samples) and the per-proposition agreement."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    # 3 samples: "The bearing failed." in all three (→ vec A), one one-off claim (→ vec B).
    p = _multi_sample_propositionizer(
        side_effect=[
            {
                "propositions": [
                    {"text": "The bearing failed."},
                    {"text": "An assembly fault occurred."},
                ]
            },
            {"propositions": [{"text": "The bearing failed."}]},
            {"propositions": [{"text": "The bearing failed."}]},
        ],
        embed_return=[
            _VEC_A,
            _VEC_B,
            _VEC_A,
            _VEC_A,
        ],  # flatten order: (s0,p0),(s0,p1),(s1,p0),(s2,p0)
    )
    # always entailed + preserved → verify component 1.0, so faithfulness == agreement
    _with_verifier(
        p,
        lambda _user: {
            "entailment": "entailed",
            "polarity_preserved": True,
            "modality_preserved": True,
            "attribution_preserved": True,
        },
    )
    await p.propositionize_document(session, doc_id, [span], raw)

    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.text, p.agreement, p.faithfulness, p.provisional",
        returns="text agtype, agr agtype, faith agtype, prov agtype",
    )
    by_text = {str(r[0]).strip('"'): r for r in rows}
    assert len(by_text) == 2  # the two stable duplicates collapsed into one proposition

    stable = by_text["The bearing failed."]
    assert float(str(stable[1])) == pytest.approx(1.0)  # agreement 3/3
    assert float(str(stable[2])) == pytest.approx(1.0)  # faithfulness = 1.0 × 1.0
    assert str(stable[3]).strip('"').lower() == "false"

    unstable = by_text["An assembly fault occurred."]
    assert float(str(unstable[1])) == pytest.approx(1 / 3)  # agreement 1/3
    assert float(str(unstable[2])) == pytest.approx(1 / 3)  # faithfulness = 1.0 × 1/3 → quarantined
    assert str(unstable[3]).strip('"').lower() == "true"

    # The extract Action records the multi-sample regime + per-proposition agreement (Trial A5).
    ext = await session.execute(
        text(
            "SELECT sampling, outputs FROM actions "
            "WHERE actor = 'propositionizer' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span.id)},
    )
    rec = ext.one()
    assert rec.sampling["n_samples"] == 3
    agreements = sorted(a["agreement"] for a in rec.outputs["agreements"])
    assert agreements[0] == pytest.approx(1 / 3)
    assert agreements[1] == pytest.approx(1.0)


async def test_multi_sample_without_verifier_sets_agreement_only(session: AsyncSession) -> None:
    """G1.3 degraded mode: multi-sample on but verifier off → agreement is still computed and
    persisted, while faithfulness stays null (faithfulness needs verification too). G1.21: a null
    faithfulness is *unassessed* → provisional, so the atom carries UNASSESSED_FAITHFULNESS and the
    legacy boolean is True (repinned from the pre-G1.21 `provisional is None`)."""
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id, span = await _seed_one_span_doc(session, raw)

    p = _multi_sample_propositionizer(
        side_effect=[
            {
                "propositions": [
                    {"text": "The bearing failed."},
                    {"text": "An assembly fault occurred."},
                ]
            },
            {"propositions": [{"text": "The bearing failed."}]},
            {"propositions": [{"text": "The bearing failed."}]},
        ],
        embed_return=[_VEC_A, _VEC_B, _VEC_A, _VEC_A],
    )
    assert p.verifier is None
    await p.propositionize_document(session, doc_id, [span], raw)

    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.text, p.agreement, p.faithfulness, p.provisional, properties(p)",
        returns="text agtype, agr agtype, faith agtype, prov agtype, props agtype",
    )
    by_text = {str(r[0]).strip('"'): r for r in rows}
    stable = by_text["The bearing failed."]
    assert float(str(stable[1])) == pytest.approx(1.0)  # agreement persisted
    assert stable[2] is None  # faithfulness null without a verifier
    # G1.21: unassessed faithfulness → provisional. The reason set is the source of truth; the
    # legacy boolean mirrors its non-emptiness.
    assert decode_provisional_reasons(parse_agtype_map(stable[4]).get("provisional_reasons")) == [
        "unassessed_faithfulness"
    ]
    assert str(stable[3]).strip('"').lower() == "true"


async def test_verifier_absent_leaves_faithfulness_null(session: AsyncSession) -> None:
    """Degraded mode (no verifier configured): no verify Action, faithfulness null. G1.21 (§3.1
    D2): unassessed faithfulness is provisional → the atom carries UNASSESSED_FAITHFULNESS and the
    legacy boolean is True (repinned from the pre-G1.21 `provisional is None`); no faithfulness is
    ever coerced toward trusted just because no verifier ran."""
    await bootstrap_session(session)
    raw = "The surface shows indentations."
    doc_id, span = await _seed_one_span_doc(session, raw)

    p = _mock_propositionizer(
        llm_return={"propositions": [{"text": "The surface shows indentations."}]},
        n_vectors=1,
    )
    assert p.verifier is None
    await p.propositionize_document(session, doc_id, [span], raw)

    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.faithfulness, p.provisional, properties(p)",
        returns="faith agtype, prov agtype, props agtype",
    )
    assert rows[0][0] is None  # faithfulness unassessed without a verifier
    assert decode_provisional_reasons(parse_agtype_map(rows[0][2]).get("provisional_reasons")) == [
        "unassessed_faithfulness"
    ]
    assert str(rows[0][1]).strip('"').lower() == "true"

    no_verify = await session.execute(
        text(
            "SELECT count(*) FROM actions "
            "WHERE actor = 'verifier' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span.id)},
    )
    assert no_verify.scalar_one() == 0


# --- G1.17 R1: per-span error isolation + resume ---


async def _seed_two_span_doc(
    session: AsyncSession, raw: str, a_end: int
) -> tuple[uuid.UUID, Span, Span]:
    """A document with two adjacent spans [0, a_end) and [a_end+1, len)."""
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": raw},
    )
    await execute_cypher(session, f"CREATE (:Document {cypher_map({'id': str(doc_id)})})")
    span_a = Span(id=uuid.uuid4(), document_id=doc_id, start=0, end=a_end)
    span_b = Span(id=uuid.uuid4(), document_id=doc_id, start=a_end + 1, end=len(raw))
    for s in (span_a, span_b):
        await execute_cypher(
            session,
            "CREATE (:Span "
            + cypher_map(
                {"id": str(s.id), "document_id": str(doc_id), "start": s.start, "end": s.end}
            )
            + ")",
        )
    await session.commit()
    return doc_id, span_a, span_b


def _failing_propositionizer(fail_marker: str) -> Propositionizer:
    """An extractor that raises on any span whose TARGET text contains ``fail_marker`` and
    returns one proposition otherwise — to exercise per-span isolation (G1.17 R1)."""
    llm = MagicMock()
    llm.model = "test-model"

    async def _guided(messages: list[dict], schema: dict, sampling: dict | None = None) -> dict:
        target = messages[1]["content"]
        if fail_marker in target:
            raise RuntimeError("extractor exploded on this span")
        return {"propositions": [{"text": "Persisted proposition."}]}

    llm.guided_complete = AsyncMock(side_effect=_guided)
    substrate = MagicMock()
    substrate.model_name = "BAAI/bge-m3"
    substrate.embed_passages = MagicMock(side_effect=lambda texts: [[0.1] * 1024 for _ in texts])
    return Propositionizer(llm, substrate, context_window=8, concurrency=4)


async def _props_on(session: AsyncSession, span: Span) -> list:
    return await execute_cypher(
        session,
        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span.id)})}) "
        "RETURN p.id",
        returns="id agtype",
    )


async def test_failed_span_is_isolated_and_resumable(session: AsyncSession) -> None:
    """G1.17 R1: one span whose extraction raises does not abort the document — the healthy
    span commits, the failure is reported, and a later run re-extracts exactly the failed span
    via the content-addressed idempotency check (it recorded no Action)."""
    await bootstrap_session(session)
    raw = "Alpha fact here. Beta fact here."
    doc_id, span_a, span_b = await _seed_two_span_doc(session, raw, a_end=16)

    report = await _failing_propositionizer("Beta").propositionize_document(
        session, doc_id, [span_a, span_b], raw
    )

    # The healthy span committed; the failing span is isolated and reported, not raised.
    assert len(report.action_ids) == 1
    assert len(report.failed_spans) == 1
    assert report.failed_spans[0].span_id == span_b.id
    assert report.failed_spans[0].phase == "infer"
    assert len(await _props_on(session, span_a)) == 1
    assert await _props_on(session, span_b) == []

    # The failed span recorded no extract Action — so the next run treats it as never-extracted.
    b_actions = await session.execute(
        text(
            "SELECT count(*) FROM actions "
            "WHERE actor = 'propositionizer' AND inputs->>'target_span' = :sid"
        ),
        {"sid": str(span_b.id)},
    )
    assert b_actions.scalar_one() == 0

    # Resume: a healthy run re-extracts only span_b (span_a no-ops via idempotency).
    resume = _mock_propositionizer(
        llm_return={"propositions": [{"text": "Beta now persists."}]}, n_vectors=1
    )
    report2 = await resume.propositionize_document(session, doc_id, [span_a, span_b], raw)
    assert len(report2.action_ids) == 1  # only span_b was pending
    assert report2.failed_spans == []
    assert len(await _props_on(session, span_b)) == 1
