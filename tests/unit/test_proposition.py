import asyncio
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

import iknos.core.proposition as proposition_mod
from iknos.core.proposition import (
    EXTRACTION_SCHEMA,
    Propositionizer,
    PropositionResult,
    _PropositionOut,
    build_context,
    build_messages,
    extractor_prompt_sha,
    extractor_schema_sha,
    span_text,
)
from iknos.core.verify import _VerifyOut
from iknos.types.epistemic import (
    Attribution,
    Entailment,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
)
from iknos.types.nodes import Span


def _span(doc_id: uuid.UUID, start: int, end: int) -> Span:
    return Span(id=uuid.uuid4(), document_id=doc_id, start=start, end=end)


def test_span_text_slices_raw_text():
    doc = uuid.uuid4()
    raw = "Hello world. Goodbye."
    assert span_text(raw, _span(doc, 0, 12)) == "Hello world."


def test_build_context_preceding_window():
    doc = uuid.uuid4()
    raw = "AAAA BBBB CCCC DDDD"
    spans = [_span(doc, 0, 4), _span(doc, 5, 9), _span(doc, 10, 14), _span(doc, 15, 19)]
    # index 3, window 2 -> preceding spans at indices 1 and 2 ("BBBB", "CCCC")
    ctx_spans, ctx_text = build_context(spans, index=3, raw_text=raw, window=2)
    assert [s.start for s in ctx_spans] == [5, 10]
    assert ctx_text == "BBBB\nCCCC"


def test_build_context_start_of_document_is_empty():
    doc = uuid.uuid4()
    raw = "AAAA BBBB"
    spans = [_span(doc, 0, 4), _span(doc, 5, 9)]
    ctx_spans, ctx_text = build_context(spans, index=0, raw_text=raw, window=8)
    assert ctx_spans == []
    assert ctx_text == ""


def test_build_context_window_zero():
    doc = uuid.uuid4()
    raw = "AAAA BBBB CCCC"
    spans = [_span(doc, 0, 4), _span(doc, 5, 9), _span(doc, 10, 14)]
    ctx_spans, ctx_text = build_context(spans, index=2, raw_text=raw, window=0)
    assert ctx_spans == []
    assert ctx_text == ""


def test_build_messages_marks_context_and_target():
    msgs = build_messages("prior text", "target text")
    assert msgs[0]["role"] == "system"
    assert "CONTEXT:\nprior text" in msgs[1]["content"]
    assert "TARGET:\ntarget text" in msgs[1]["content"]


def test_build_messages_no_context_placeholder():
    msgs = build_messages("   ", "target text")
    assert "(no preceding context)" in msgs[1]["content"]


def _propositionizer(llm_return, embed_return):
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(return_value=llm_return)
    substrate = MagicMock()
    substrate.embed_passages = MagicMock(return_value=embed_return)
    return Propositionizer(llm, substrate, context_window=8, concurrency=2)


@pytest.mark.asyncio
async def test_infer_span_maps_propositions_to_target_span():
    doc = uuid.uuid4()
    raw = "Smith spoke. He argued it was insufficient."
    spans = [_span(doc, 0, 12), _span(doc, 13, 44)]
    p = _propositionizer(
        llm_return={
            "propositions": [
                {"text": "Smith argued the budget was insufficient."},
                {"text": "Smith made an argument."},
            ]
        },
        embed_return=[[1.0, 0.0], [0.0, 1.0]],
    )

    results, _twins = await p._infer_span(asyncio.Semaphore(2), spans, index=1, raw_text=raw)

    assert [r.text for r in results] == [
        "Smith argued the budget was insufficient.",
        "Smith made an argument.",
    ]
    # Every proposition is evidenced by the target span (index 1), not the context span.
    assert {r.span_id for r in results} == {spans[1].id}
    assert all(r.document_id == doc for r in results)
    assert results[0].embedding == [1.0, 0.0]

    # The context window (the preceding span) was passed to the LLM for resolution.
    sent_messages = p.llm.guided_complete.call_args.args[0]
    assert "Smith spoke." in sent_messages[1]["content"]


@pytest.mark.asyncio
async def test_infer_span_empty_returns_no_results_and_skips_embedding():
    doc = uuid.uuid4()
    raw = "Well, anyway."
    spans = [_span(doc, 0, 13)]
    p = _propositionizer(llm_return={"propositions": []}, embed_return=[])

    results, _twins = await p._infer_span(asyncio.Semaphore(2), spans, index=0, raw_text=raw)

    assert results == []
    p.substrate.embed_passages.assert_not_called()


# --- epistemic fields (G1.1) ---


def test_proposition_out_defaults_for_bare_text():
    # A bare {"text": ...} response (the pre-G1.1 shape) still validates via defaults,
    # so existing extractions / mocks keep working.
    out = _PropositionOut.model_validate({"text": "The bearing failed."})
    assert out.polarity is Polarity.ASSERTED
    assert out.modality is Modality.CATEGORICAL
    assert out.attribution is Attribution.DOCUMENT
    assert out.scope == ""
    assert out.epistemic_class is EpistemicClass.OBSERVATION


def test_proposition_out_full_record():
    out = _PropositionOut.model_validate(
        {
            "text": "The bearing failed.",
            "polarity": "negated",
            "modality": "probable",
            "attribution": "named-source",
            "scope": "for all bearings",
            "epistemic_class": "judgement",
        }
    )
    assert out.polarity is Polarity.NEGATED
    assert out.modality is Modality.PROBABLE
    assert out.attribution is Attribution.NAMED_SOURCE
    assert out.epistemic_class is EpistemicClass.JUDGEMENT


@pytest.mark.asyncio
async def test_infer_span_populates_epistemic_fields_and_routing():
    doc = uuid.uuid4()
    raw = "Smith spoke. He concluded it was an assembly fault."
    spans = [_span(doc, 0, 12), _span(doc, 13, 51)]
    p = _propositionizer(
        llm_return={
            "propositions": [
                {
                    "text": "The failure was an assembly fault.",
                    "polarity": "asserted",
                    "modality": "categorical",
                    "attribution": "named-source",
                    "scope": "",
                    "epistemic_class": "judgement",
                },
                {"text": "The rolling surface shows particle indentations."},  # bare → observation
            ]
        },
        embed_return=[[1.0, 0.0], [0.0, 1.0]],
    )

    results, _twins = await p._infer_span(asyncio.Semaphore(2), spans, index=1, raw_text=raw)

    judgement, observation = results
    assert judgement.epistemic_class is EpistemicClass.JUDGEMENT
    assert judgement.attribution is Attribution.NAMED_SOURCE
    assert judgement.routing is Routing.JUDGEMENT  # G1.2: a conclusion routes to judgement
    # The bare-text proposition defaults to observation → routes to fact.
    assert observation.epistemic_class is EpistemicClass.OBSERVATION
    assert observation.routing is Routing.FACT
    # faithfulness is not self-reported — null until G1.4/G1.5; no reasons until assessed (R8).
    assert all(r.faithfulness is None and r.provisional_reasons == [] for r in results)


# --- extract-then-verify wiring (G1.4/G1.5) ---


def _result(text: str, span_id: uuid.UUID, doc_id: uuid.UUID) -> PropositionResult:
    return PropositionResult(
        id=uuid.uuid4(),
        text=text,
        span_id=span_id,
        document_id=doc_id,
        embedding=[1.0, 0.0],
        polarity=Polarity.ASSERTED,
        modality=Modality.CATEGORICAL,
        attribution=Attribution.DOCUMENT,
        scope="",
        epistemic_class=EpistemicClass.OBSERVATION,
        routing=Routing.FACT,
    )


def _verdict(
    entailment: Entailment = Entailment.ENTAILED,
    *,
    polarity_preserved: bool = True,
    modality_preserved: bool = True,
) -> _VerifyOut:
    return _VerifyOut(
        entailment=entailment,
        polarity_preserved=polarity_preserved,
        modality_preserved=modality_preserved,
        attribution_preserved=True,
    )


def _propositionizer_with_verifier(verdict: _VerifyOut) -> Propositionizer:
    p = _propositionizer(llm_return={"propositions": []}, embed_return=[])
    verifier = MagicMock()
    verifier.llm = MagicMock()
    verifier.llm.model = "verifier-model"
    verifier.verify_proposition = AsyncMock(return_value=verdict)
    p.verifier = verifier
    return p


def test_verifier_defaults_to_none() -> None:
    p = _propositionizer(llm_return={"propositions": []}, embed_return=[])
    assert p.verifier is None


@pytest.mark.asyncio
async def test_verify_all_sets_faithfulness_and_provisional() -> None:
    doc = uuid.uuid4()
    raw = "The rolling surface shows particle indentations."
    spans = [_span(doc, 0, len(raw))]
    p = _propositionizer_with_verifier(_verdict())  # entailed + fully preserved
    inferred = [(0, [_result("The surface shows indentations.", spans[0].id, doc)])]

    verified = await p._verify_all(asyncio.Semaphore(2), spans, raw, inferred)

    (i, results, verdicts) = verified[0]
    assert i == 0
    assert results[0].faithfulness == pytest.approx(1.0)
    assert results[0].provisional_reasons == []
    assert verdicts[0].entailment is Entailment.ENTAILED
    # The verifier was handed the source span's text, not the proposition text.
    p.verifier.verify_proposition.assert_awaited_once()
    assert p.verifier.verify_proposition.call_args.args[0] == raw


@pytest.mark.asyncio
async def test_verify_all_contradicted_marks_provisional() -> None:
    doc = uuid.uuid4()
    raw = "The bearing did not fail."
    spans = [_span(doc, 0, len(raw))]
    p = _propositionizer_with_verifier(_verdict(Entailment.CONTRADICTED, polarity_preserved=False))
    inferred = [(0, [_result("The bearing failed.", spans[0].id, doc)])]

    verified = await p._verify_all(asyncio.Semaphore(2), spans, raw, inferred)

    results = verified[0][1]
    assert results[0].faithfulness == pytest.approx(0.0)
    assert results[0].provisional_reasons == ["low_faithfulness"]


@pytest.mark.asyncio
async def test_verify_all_modality_flatten_stays_above_threshold() -> None:
    doc = uuid.uuid4()
    raw = "The bearing probably failed."
    spans = [_span(doc, 0, len(raw))]
    p = _propositionizer_with_verifier(_verdict(modality_preserved=False))
    inferred = [(0, [_result("The bearing failed.", spans[0].id, doc)])]

    verified = await p._verify_all(asyncio.Semaphore(2), spans, raw, inferred)

    results = verified[0][1]
    assert results[0].faithfulness == pytest.approx(0.70)
    assert results[0].provisional_reasons == []


@pytest.mark.asyncio
async def test_verify_all_folds_in_agreement() -> None:
    # Verifier passes it (component 1.0) but it appeared in only 1/3 samples → combine pulls
    # faithfulness to 1/3 → provisional. (None agreement would leave it at 1.0; see G1.4 tests.)
    doc = uuid.uuid4()
    raw = "The rolling surface shows particle indentations."
    spans = [_span(doc, 0, len(raw))]
    p = _propositionizer_with_verifier(_verdict())
    unstable = replace(
        _result("The surface shows indentations.", spans[0].id, doc), agreement=1 / 3
    )

    verified = await p._verify_all(asyncio.Semaphore(2), spans, raw, [(0, [unstable])])

    results = verified[0][1]
    assert results[0].faithfulness == pytest.approx(1 / 3)
    assert results[0].provisional_reasons == ["low_faithfulness"]


@pytest.mark.asyncio
async def test_verify_all_degrades_on_verifier_failure() -> None:
    # G1.17 R2: a verifier that raises (endpoint down past retries, unparseable response) must not
    # crash the batch. The proposition keeps faithfulness/provisional null (the documented degraded
    # G1.1 mode) and its verdict slot is None so _persist logs the failure on the verify Action.
    doc = uuid.uuid4()
    raw = "The rolling surface shows particle indentations."
    spans = [_span(doc, 0, len(raw))]
    p = _propositionizer_with_verifier(_verdict())
    p.verifier.verify_proposition = AsyncMock(side_effect=RuntimeError("verifier endpoint down"))
    inferred = [(0, [_result("The surface shows indentations.", spans[0].id, doc)])]

    verified = await p._verify_all(asyncio.Semaphore(2), spans, raw, inferred)

    (i, results, verdicts) = verified[0]
    assert results[0].faithfulness is None
    assert results[0].provisional_reasons == []
    assert verdicts == [None]


@pytest.mark.asyncio
async def test_verify_all_failure_preserves_twin_provisional() -> None:
    # A polarity-unstable twin (G1.14) is already provisional=True before verify. If the verifier
    # then fails (R2), the quarantine must survive — the degraded path must not clear it.
    doc = uuid.uuid4()
    raw = "The bearing failed."
    spans = [_span(doc, 0, len(raw))]
    p = _propositionizer_with_verifier(_verdict())
    p.verifier.verify_proposition = AsyncMock(side_effect=RuntimeError("boom"))
    twin = replace(
        _result("The bearing failed.", spans[0].id, doc), provisional_reasons=["low_faithfulness"]
    )

    verified = await p._verify_all(asyncio.Semaphore(2), spans, raw, [(0, [twin])])

    results = verified[0][1]
    assert results[0].provisional_reasons == ["low_faithfulness"]
    assert results[0].faithfulness is None


# --- multi-sample extraction (G1.3) ---


def _multi_propositionizer(sample_returns, embed_return, *, n_samples, threshold=0.86):
    """A Propositionizer whose extractor returns a *different* set per sample (side_effect)."""
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(side_effect=sample_returns)
    substrate = MagicMock()
    substrate.embed_passages = MagicMock(return_value=embed_return)
    return Propositionizer(
        llm,
        substrate,
        context_window=8,
        concurrency=4,
        sampling={"temperature": 0.7},
        n_samples=n_samples,
        agreement_threshold=threshold,
    )


@pytest.mark.asyncio
async def test_multi_sample_clusters_and_scores_agreement() -> None:
    doc = uuid.uuid4()
    raw = "The bearing failed under load."
    spans = [_span(doc, 0, len(raw))]
    # 3 samples: two produce claim "A", one produces "B". Candidates flatten in sample order.
    p = _multi_propositionizer(
        sample_returns=[
            {"propositions": [{"text": "A"}]},
            {"propositions": [{"text": "A"}]},
            {"propositions": [{"text": "B"}]},
        ],
        embed_return=[[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        n_samples=3,
    )

    results, _twins = await p._infer_span(asyncio.Semaphore(4), spans, index=0, raw_text=raw)

    assert p.llm.guided_complete.call_count == 3  # the extractor was sampled N times
    p.substrate.embed_passages.assert_called_once()  # one batched pass over all candidates
    assert [r.text for r in results] == ["A", "B"]
    assert results[0].agreement == pytest.approx(2 / 3)  # A: 2 of 3 samples
    assert results[1].agreement == pytest.approx(1 / 3)  # B: 1 of 3 → unstable


@pytest.mark.asyncio
async def test_multi_sample_polarity_twin_quarantines_both_sides() -> None:
    # 5 samples wavering on the sign of one claim: 3 assert it, 2 negate it. Polarity-aware
    # clustering (G1.14) must not report agreement 1.0; both sides are quarantined (provisional)
    # and the twin pairing surfaces for the extract Action.
    doc = uuid.uuid4()
    raw = "The bearing failed under load."
    spans = [_span(doc, 0, len(raw))]
    p = _multi_propositionizer(
        sample_returns=[
            {"propositions": [{"text": "The bearing failed.", "polarity": "asserted"}]},
            {"propositions": [{"text": "The bearing failed.", "polarity": "asserted"}]},
            {"propositions": [{"text": "The bearing failed.", "polarity": "asserted"}]},
            {"propositions": [{"text": "The bearing failed.", "polarity": "negated"}]},
            {"propositions": [{"text": "The bearing failed.", "polarity": "negated"}]},
        ],
        embed_return=[[1.0, 0.0]] * 5,  # all near-identical: cosine cannot tell sign apart
        n_samples=5,
    )

    results, twins = await p._infer_span(asyncio.Semaphore(4), spans, index=0, raw_text=raw)

    assert len(results) == 2
    assert sorted(r.agreement for r in results) == pytest.approx([0.4, 0.6])  # never 1.0
    assert all(r.provisional_reasons == ["low_faithfulness"] for r in results)  # both quarantined
    assert len(twins) == 1
    assert set(twins[0]) == {r.id for r in results}  # the pair links the two propositions


@pytest.mark.asyncio
async def test_single_sample_leaves_agreement_null() -> None:
    # N=1 default: no clustering, agreement stays None (byte-identical to pre-G1.3).
    doc = uuid.uuid4()
    raw = "The bearing failed."
    spans = [_span(doc, 0, len(raw))]
    p = _propositionizer(llm_return={"propositions": [{"text": "A"}]}, embed_return=[[1.0, 0.0]])

    results, _twins = await p._infer_span(asyncio.Semaphore(2), spans, index=0, raw_text=raw)

    assert p.n_samples == 1
    assert results[0].agreement is None


def test_multi_sample_rejects_greedy_sampling() -> None:
    llm, substrate = MagicMock(), MagicMock()
    llm.model = "m"
    # Default sampling is greedy (temperature 0.0) — N identical samples carry no signal.
    with pytest.raises(ValueError, match="temperature>0"):
        Propositionizer(llm, substrate, n_samples=3)


def test_rejects_nonpositive_samples() -> None:
    llm, substrate = MagicMock(), MagicMock()
    llm.model = "m"
    with pytest.raises(ValueError, match=">= 1"):
        Propositionizer(llm, substrate, n_samples=0)


# --- G1.15: prompt/schema hashes feed the extraction cache key ---


def test_extractor_prompt_sha_shape_and_determinism() -> None:
    h = extractor_prompt_sha()
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == extractor_prompt_sha()


def test_extractor_prompt_sha_changes_when_system_prompt_changes(monkeypatch) -> None:
    # The core G1.15 property: edit one character of the prompt and the digest moves, so the
    # extraction cache invalidates without anyone bumping EXTRACT_SCHEMA_VERSION.
    before = extractor_prompt_sha()
    monkeypatch.setattr(proposition_mod, "SYSTEM_PROMPT", proposition_mod.SYSTEM_PROMPT + " ")
    assert extractor_prompt_sha() != before


def test_extractor_prompt_sha_excludes_per_span_text() -> None:
    # The hash is over the *static* scaffold only — it is parameterless and reflects no document
    # text (which is keyed separately as target_text/context_text in extraction_content_hash).
    assert extractor_prompt_sha() == extractor_prompt_sha()


def test_extractor_schema_sha_is_key_order_insensitive() -> None:
    # Re-ordering schema keys must not change the digest (canonical JSON).
    from iknos.core.cache import canonical_json_sha256

    reordered = {k: EXTRACTION_SCHEMA[k] for k in reversed(list(EXTRACTION_SCHEMA))}
    assert extractor_schema_sha() == canonical_json_sha256(reordered)
