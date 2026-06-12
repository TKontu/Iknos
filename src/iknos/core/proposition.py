"""Proposition layer (Phase 1, Increment 3) — the "value layer" (§3).

Decontextualizes each sub-paragraph Span into atomic, self-contained Propositions
(resolve pronouns, attach qualifiers, split compound claims), links each back to
its source Span via EVIDENCED_BY, indexes the text densely (pgvector) and sparsely
(lexical-exact), and records an Action per span (§10.1).

Scope (per the increment decision): Span vertices are assumed already present in
AGE; materializing them is a separate follow-up. Provenance links to the *target*
span only; the preceding-K-span context window is used to resolve references and
its span ids are recorded in Action.inputs for audit, but is not itself evidence.

Concurrency: the shared AsyncSession is not safe for concurrent use, so the run is
three-phase — (1) serial idempotency filter, (2) concurrent inference with no DB
access, (3) serial per-span persistence. Only the slow LLM/embedding work runs
concurrently; each span's writes commit in their own short transaction.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.cache import canonical_json_sha256, extraction_content_hash
from iknos.core.consistency import (
    DEFAULT_AGREEMENT_THRESHOLD,
    Candidate,
    consolidate_samples,
    require_sampling_diversity,
)
from iknos.core.embeddings import EmbeddingBackend, EmbeddingModelMismatchError
from iknos.core.llm import LLMClient
from iknos.core.parse import parse_quality_factor, worst_source_quality
from iknos.core.prompts import vocab
from iknos.core.reuse import ReusableExtraction, find_reusable_extraction
from iknos.db.orm import PropositionEmbedding
from iknos.provenance.action_log import record_action
from iknos.provenance.metrics import elapsed_ms, llm_metrics
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    ProvisionalReason,
    Routing,
    combine_faithfulness,
    decode_provisional_reasons,
    faithfulness_from_verdict,
    legacy_provisional,
    merge_provisional_reasons,
    provisional_reasons_for,
    reassess_faithfulness_reasons,
    route_for,
)
from iknos.types.nodes import Span

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only: verify.py imports PropositionResult from here,
    # so a runtime import would be circular. The Verifier is injected and _VerifyOut
    # objects are only read (attribute access), so neither is needed at runtime.
    from iknos.core.verify import Verifier, _VerifyOut

# Note: iknos.db.age is imported lazily inside _persist so that importing this
# module does not pull in the config singleton (DATABASE_URL) — unit tests of the
# inference path stay DB-free.


SYSTEM_PROMPT = (
    "You convert a TARGET passage into atomic, self-contained factual propositions, "
    "each tagged with its epistemic operators as structured fields (never folded into "
    "the text).\n"
    "Rules:\n"
    "- Use CONTEXT only to resolve references (pronouns, definite descriptions, "
    "ellipsis). Do not extract claims that appear only in the context.\n"
    "- Emit one proposition per independent claim asserted in the TARGET; split "
    "compound sentences.\n"
    "- Rewrite each proposition to stand alone: replace pronouns with their "
    "referents and attach necessary qualifiers (dates, places, entities) so it is "
    "understandable without the surrounding text.\n"
    "- Preserve meaning; add no facts not supported by the TARGET (context aids "
    "reference resolution only).\n"
    "- `text` holds the AFFIRMATIVE content; `polarity` carries the sign. Write a "
    'denial as affirmative text + polarity=negated (e.g. "the bearing did not fail" '
    '-> text "The bearing failed.", polarity negated) — never double-negate the text.\n'
    "Per-proposition fields:\n"
    f"- polarity ({vocab(Polarity)}): whether the content is asserted or denied.\n"
    f"- modality ({vocab(Modality)}): the claim's certainty.\n"
    f"- attribution ({vocab(Attribution)}): asserted by the document itself, conveyed "
    "as reported speech, or a named source's claim.\n"
    "- scope: brief quantifier-scope notes, or empty string if none.\n"
    f"- epistemic_class ({vocab(EpistemicClass)}): an objective observation/measurement, "
    "vs testimony of an event, vs a judgement/interpretation. Orthogonal to modality "
    "(a categorical claim can still be a judgement). Extract observations; classify a "
    "source's conclusions as judgement, do not assert them as fact.\n"
    "- If the TARGET asserts no factual claim, return an empty list.\n"
    'Example: "The operator claimed the bearing probably didn\'t fail" -> '
    '{"text": "The bearing failed.", "polarity": "negated", "modality": "probable", '
    '"attribution": "named-source", "scope": "", "epistemic_class": "judgement"}.\n'
    'Return JSON of the form {"propositions": [{"text": "...", "polarity": "...", '
    '"modality": "...", "attribution": "...", "scope": "...", "epistemic_class": "..."}]}.'
)


class _PropositionOut(BaseModel):
    """One proposition as emitted by the extractor (drives guided decoding).

    The model emits only the **descriptive** epistemic fields it can classify;
    defaults keep a bare ``{"text": ...}`` response valid. ``faithfulness`` is
    deliberately absent — it is calibrated by verification (G1.4/G1.5), not
    self-reported (§3.1); ``provisional``/``routing`` are derived, not emitted.
    """

    text: str
    polarity: Polarity = Polarity.ASSERTED
    modality: Modality = Modality.CATEGORICAL
    attribution: Attribution = Attribution.DOCUMENT
    scope: str = ""
    epistemic_class: EpistemicClass = EpistemicClass.OBSERVATION


class PropositionExtraction(BaseModel):
    """Structured output contract; drives vLLM guided decoding."""

    propositions: list[_PropositionOut]


EXTRACTION_SCHEMA = PropositionExtraction.model_json_schema()

# A *semantic* version of the extractor's output shape. Since G1.15 it no longer carries cache
# invalidation alone — `prompt_sha`/`schema_sha` (below) hash the *actual* prompt + schema, so a
# reworded prompt or changed schema re-extracts even without a bump here. Keep bumping it for a
# deliberate, human-legible "the output contract changed" marker (it stays in the cache key, and is
# stored on the extract Action alongside the hash for debuggability). Mirrors
# core/ingest.py::SEGMENT_SCHEMA_VERSION.
EXTRACT_SCHEMA_VERSION = 1


class StaleExtractionError(Exception):
    """A span was re-run under a different extraction pipeline than its prior run (G1.7).

    The idempotency key is ``(span_id, content_hash)``, where the hash covers the extractor
    model, prompt/schema version, sampling regime, and context (its text + the ordered context
    span ids) — **not** the verifier, which keys its own stage since G1.22 (core/cache.py).
    An identical re-run is a true no-op; a span whose stored hash differs from the current one
    means the model/prompt/regime/context changed since it was extracted. With
    ``cascade_reextract=True`` (the G1.7r default) such a span is **re-extracted** — its
    superseded propositions purged first; this error is raised only when cascade is **disabled**
    (the conservative refuse-to-overwrite mode) — mirrors
    ``core/ingest.py::DocumentResegmentationError``.
    """


class CascadeDependentsError(StaleExtractionError):
    """A stale span's propositions already feed downstream nodes; cascade refuses (G1.7r).

    Cascade re-extraction (``cascade_reextract=True``) purges a span's superseded **Phase-1**
    perception output (the propositions + their ``EVIDENCED_BY`` edges + dense/lexical index
    rows). A proposition that some later node already derives from has an edge **beyond** its lone
    ``EVIDENCED_BY`` → ``Span``; ``DETACH DELETE``-ing it would silently orphan that consumer. The
    full downstream cascade (purge-and-re-derive the dependents too) is the deferred
    resegmentation-cascade work (``core/ingest.py``), so until it lands this **fails loud** rather
    than corrupt the graph. A subclass of :class:`StaleExtractionError` so existing
    whole-document fail-loud handlers still treat it as fatal.
    """


@dataclass(frozen=True)
class PropositionResult:
    """One extracted proposition with its provenance, epistemic fields, and vector.

    ``routing`` is derived from ``epistemic_class`` (G1.2). ``faithfulness`` is set by the verify
    fan-out (G1.4/G1.5) — null when no verifier is configured; ``provisional_reasons`` (R8) is the
    accumulating quarantine-reason set (empty = not provisional). ``agreement`` is
    the multi-sample consistency signal (G1.3): the fraction of the N samples that produced this
    proposition; ``None`` in single-sample mode (N=1), where it would be a trivial 1.0. It is
    persisted on the node regardless of the verifier; when a verifier *is* present it also folds
    into ``faithfulness`` via :func:`~iknos.types.epistemic.combine_faithfulness`.
    """

    id: uuid.UUID
    text: str
    span_id: uuid.UUID  # the target/source Span
    document_id: uuid.UUID
    embedding: list[float]
    polarity: Polarity
    modality: Modality
    attribution: Attribution
    scope: str
    epistemic_class: EpistemicClass
    routing: Routing
    faithfulness: float | None = None
    provisional_reasons: list[str] = field(default_factory=list)
    agreement: float | None = None


def _with_faithfulness_reason(r: PropositionResult) -> PropositionResult:
    """OR-fold the faithfulness-axis provisional reason onto a *finalized* result (R8/G1.21).

    The single enforcement point for "faithfulness → provisional" once the verify decision is
    settled: :func:`~iknos.types.epistemic.provisional_reasons_for` maps a real score below the
    gate to ``LOW_FAITHFULNESS`` and a **null** score (verifier off, or unavailable for this
    proposition — the degraded mode) to ``UNASSESSED_FAITHFULNESS`` (§3.1 D2 / G1.21: unassessed
    grounding is provisional, never coerced toward trusted). OR-folded onto the result's existing
    reasons (a G1.14 twin keeps ``POLARITY_UNSTABLE``), never clearing them. Idempotent — the
    merge dedupes — so it is safe to re-apply on an already-scored verify-success result. Returns
    the same object unchanged when the fold is a no-op, so the frozen result is only rebuilt when
    a reason is actually added.
    """
    folded = merge_provisional_reasons(
        r.provisional_reasons, provisional_reasons_for(r.faithfulness)
    )
    if folded == r.provisional_reasons:
        return r
    return replace(r, provisional_reasons=folded)


@dataclass(frozen=True)
class FailedSpan:
    """A span whose extraction (or persistence) raised, isolated so the run continues (G1.17 R1).

    The error is stringified (not the live exception) so the report is a plain serializable value.
    The span recorded **no** extract Action, so the next run's content-addressed idempotency
    (Phase 1) sees no stored hash for it and re-extracts — resume is free, no bespoke retry queue.
    """

    span_id: uuid.UUID
    phase: str  # "infer" | "persist" — where it failed
    error: str


@dataclass(frozen=True)
class PropositionizeReport:
    """Outcome of a document run: the extract Action ids, plus any spans that failed in isolation.

    ``action_ids`` are the per-span extract Actions committed this run (empty on a full no-op
    re-run). ``failed_spans`` is empty on a clean run; a non-empty list means the document is
    *partially* ingested and a re-run will pick up exactly the failed spans via idempotency
    (G1.17 R1).
    """

    action_ids: list[uuid.UUID]
    failed_spans: list[FailedSpan]


def span_text(raw_text: str, span: Span) -> str:
    """The document substring a Span points at."""
    return raw_text[span.start : span.end]


def build_context(
    spans: list[Span], index: int, raw_text: str, window: int
) -> tuple[list[Span], str]:
    """The preceding `window` spans and their concatenated text (for reference resolution)."""
    start = max(0, index - window)
    context_spans = spans[start:index]
    context_text = "\n".join(span_text(raw_text, s) for s in context_spans)
    return context_spans, context_text


def build_messages(context_text: str, target_text: str) -> list[dict[str, str]]:
    """Assemble the chat messages for one span."""
    context_block = context_text.strip() or "(no preceding context)"
    user = f"CONTEXT:\n{context_block}\n\nTARGET:\n{target_text}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# Sentinels marking where the per-span CONTEXT/TARGET text goes when we render the prompt scaffold
# for hashing (G1.15). They stand in for the variable content — already keyed separately as
# context_text/target_text — so the digest covers only the *static* scaffold. NUL-wrapped so they
# can never collide with real document text.
_PROMPT_CONTEXT_SENTINEL = "\x00CONTEXT\x00"
_PROMPT_TARGET_SENTINEL = "\x00TARGET\x00"


def extractor_prompt_sha() -> str:
    """SHA-256 of the *static* extractor prompt scaffold (G1.15, review A4).

    Renders ``build_messages`` with fixed sentinels in the per-span slots and hashes the whole
    message list, so the digest covers ``SYSTEM_PROMPT`` **and** the user-message template (the
    ``CONTEXT:``/``TARGET:`` wrapper) while excluding the per-span text (which is keyed separately).
    Any reword of the prompt or the scaffold moves this digest, invalidating the extraction cache
    without anyone remembering to bump ``EXTRACT_SCHEMA_VERSION`` — the exact silent-staleness class
    G1.7 set out to close. Computed from module constants, so it is pure and cheap.
    """
    scaffold = build_messages(_PROMPT_CONTEXT_SENTINEL, _PROMPT_TARGET_SENTINEL)
    return canonical_json_sha256(scaffold)


def extractor_schema_sha() -> str:
    """SHA-256 of the canonical guided-decode schema (G1.15) — key-order-insensitive."""
    return canonical_json_sha256(EXTRACTION_SCHEMA)


# Computed once from the module-level prompt/schema constants. They are the G1.15 additions to the
# content-addressed cache key (core/cache.py::extraction_content_hash): a prompt/schema edit changes
# these even if EXTRACT_SCHEMA_VERSION is left untouched.
EXTRACTOR_PROMPT_SHA = extractor_prompt_sha()
EXTRACTOR_SCHEMA_SHA = extractor_schema_sha()


class Propositionizer:
    def __init__(
        self,
        llm: LLMClient,
        substrate: EmbeddingBackend,
        *,
        context_window: int = 8,
        concurrency: int = 8,
        sampling: dict[str, object] | None = None,
        verifier: "Verifier | None" = None,
        n_samples: int = 1,
        agreement_threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
        reuse_extractions: bool = True,
        cascade_reextract: bool = True,
    ) -> None:
        self.llm = llm
        self.substrate = substrate
        self.context_window = context_window
        self.concurrency = concurrency
        self.sampling = sampling or {"temperature": 0.0}
        # Cross-document "extract once" reuse (G1.7b): a never-extracted span whose pipeline hash
        # matches a prior committed extraction replays that extraction's propositions instead of
        # re-running the LLM (core/reuse.py). On by default — it is purely additive and sound (the
        # hash carries the full pipeline identity) — but a flag so a deploy can fall back to always
        # re-extracting without a code change. Each replayed span still records its own extract
        # Action, so the per-span idempotency key is preserved.
        self.reuse_extractions = reuse_extractions
        # Cascade re-extraction (G1.7r): when a span's stored pipeline hash differs (a model/
        # prompt/regime/verifier change), purge its superseded propositions + their edges/index
        # rows and re-extract, instead of the G1.7 fail-loud StaleExtractionError. On by default
        # — a pipeline change *should* re-derive — but a flag so a deploy can restore the
        # conservative refuse-to-overwrite behaviour. The purge is bounded to Phase-1 perception
        # output: a span whose propositions already feed downstream (Phase-2) nodes raises
        # CascadeDependentsError rather than silently orphaning them (the full downstream cascade
        # is the deferred resegmentation-cascade work).
        self.cascade_reextract = cascade_reextract
        # Optional independent verifier (G1.4). Absent → faithfulness/provisional stay null
        # (the documented G1.1 state); production wires it from settings when configured.
        self.verifier = verifier
        # Multi-sample extraction (G1.3): sample the extractor n_samples times and score each
        # proposition by cross-sample agreement. n_samples=1 is a strict no-op (no clustering,
        # agreement null) — byte-identical to single-pass. n_samples>1 with a greedy (temperature 0)
        # regime is a misconfiguration — the N samples would be identical, so agreement is vacuously
        # 1.0 — and the guard fails loud at construction (G1.23, single source of truth in
        # consistency.require_sampling_diversity; the G4.3 edge judge's temp-0 path is exempt by
        # design, documented there).
        require_sampling_diversity(n_samples, self.sampling)
        self.n_samples = n_samples
        self.agreement_threshold = agreement_threshold

    async def _infer_span(
        self, sem: asyncio.Semaphore, spans: list[Span], index: int, raw_text: str
    ) -> tuple[list[PropositionResult], list[tuple[uuid.UUID, uuid.UUID]], dict[str, Any]]:
        """Extract one span's propositions, scoring each by cross-sample agreement (G1.3/G1.14).

        Samples the extractor ``n_samples`` times, embeds every candidate, then consolidates
        semantically-equivalent extractions **within each ``(polarity, epistemic_class)``
        partition** (G1.14 — cosine cannot tell a claim from its negation); each cluster becomes
        one proposition carrying its medoid's text+operators and the fraction of samples that
        produced it (``agreement``). No DB access — concurrent-phase safe. The semaphore wraps each
        *individual* sample call (not the whole span), so the N samples of one span share the global
        budget with every other in-flight call and never deadlock by holding an outer permit while
        awaiting inner ones — the same permit discipline as ``_verify_all``.

        Returns ``(results, twin_pairs, metrics)`` where ``twin_pairs`` are ``(id, id)`` proposition
        pairs the sampler wavered the *sign* of (polarity twins, G1.14) — both flagged
        ``provisional`` and recorded on the extract ``Action``. n_samples=1 short-circuits the
        clustering (1:1 candidate→proposition, ``agreement`` null, no twins), so single-pass
        behavior is byte-identical to pre-G1.3. ``metrics`` is the R12 cost payload for the extract
        ``Action``: the wall-clock of the N-sample gather (a ``time.monotonic()`` delta covering
        only the LLM fan-out, not the downstream embed) and the summed token usage across the
        samples that reported it; ``n_samples`` is ``self.n_samples`` and ``cache_hit`` is ``False``
        (a fresh extraction — the G1.7b replay path carries its own cache-hit metrics).
        """
        target = spans[index]
        _, context_text = build_context(spans, index, raw_text, self.context_window)
        messages = build_messages(context_text, span_text(raw_text, target))

        sample_usages: list[dict[str, int]] = [{} for _ in range(self.n_samples)]

        async def sample_once(slot: int) -> list[_PropositionOut]:
            async with sem:
                raw = await self.llm.guided_complete(
                    messages, EXTRACTION_SCHEMA, self.sampling, usage_out=sample_usages[slot]
                )
            return PropositionExtraction.model_validate(raw).propositions

        t0 = time.monotonic()
        samples = await asyncio.gather(*(sample_once(i) for i in range(self.n_samples)))
        metrics = llm_metrics(
            duration_ms=elapsed_ms(t0),
            usages=sample_usages,
            n_samples=self.n_samples,
            cache_hit=False,
        )

        # Flatten the N samples into candidates, preserving (sample_index, position) so the
        # downstream clustering/medoid order is deterministic.
        flat = [
            (s_idx, pos, p) for s_idx, props in enumerate(samples) for pos, p in enumerate(props)
        ]
        if not flat:
            return [], [], metrics

        # One batched torch forward pass over every candidate (off the event loop so concurrent
        # LLM calls keep flowing). The medoid's vector is reused at persist time — no re-embed.
        vectors = await asyncio.to_thread(
            self.substrate.embed_passages, [p.text for _, _, p in flat]
        )
        candidates = [
            Candidate(
                text=p.text,
                polarity=p.polarity,
                modality=p.modality,
                attribution=p.attribution,
                scope=p.scope,
                epistemic_class=p.epistemic_class,
                embedding=v,
                sample_index=s_idx,
                position=pos,
            )
            for (s_idx, pos, p), v in zip(flat, vectors, strict=True)
        ]

        def _to_result(
            canonical: Candidate, agreement: float | None, *, provisional_reasons: list[str]
        ) -> PropositionResult:
            return PropositionResult(
                id=uuid.uuid4(),
                text=canonical.text,
                span_id=target.id,
                document_id=target.document_id,
                embedding=canonical.embedding,
                polarity=canonical.polarity,
                modality=canonical.modality,
                attribution=canonical.attribution,
                scope=canonical.scope,
                epistemic_class=canonical.epistemic_class,
                # routing derived now (G1.2); faithfulness set by the verify pass (G1.4).
                # provisional_reasons is seeded here only with POLARITY_UNSTABLE for G1.14
                # twins — the verify pass OR-folds its own faithfulness reason in, never
                # clearing it (R8 "never cleared" discipline).
                routing=route_for(canonical.epistemic_class),
                agreement=agreement,
                provisional_reasons=provisional_reasons,
            )

        # Single-pass: no clustering — each extraction is its own proposition (agreement null, no
        # polarity twins possible), exactly as before G1.3.
        if self.n_samples == 1:
            results = [_to_result(c, None, provisional_reasons=[]) for c in candidates]
            return results, [], metrics

        # Multi-sample: polarity-aware consolidation + twin detection (G1.14).
        consolidated, twin_idx = consolidate_samples(
            candidates, n_samples=self.n_samples, threshold=self.agreement_threshold
        )
        results = [
            _to_result(
                c.canonical,
                c.agreement,
                provisional_reasons=(
                    [ProvisionalReason.POLARITY_UNSTABLE.value] if c.polarity_unstable else []
                ),
            )
            for c in consolidated
        ]
        twin_pairs = [(results[i].id, results[j].id) for i, j in twin_idx]
        return results, twin_pairs, metrics

    async def _verify_all(
        self,
        sem: asyncio.Semaphore,
        spans: list[Span],
        raw_text: str,
        inferred: list[tuple[int, list[PropositionResult]]],
    ) -> list[tuple[int, list[PropositionResult], list["_VerifyOut | None"], dict[str, Any]]]:
        """Verify each inferred proposition against its source span (G1.4) and attach the
        derived faithfulness/provisional (G1.5).

        A separate concurrent fan-out, run after extraction completes and bounded by the
        *same* semaphore — each verify call acquires its own permit, so it never nests
        inside an extract permit (which would serialize throughput). Returns, per span, the
        results re-scored, the raw verdicts for the verify Action, and that span's R12 verify
        metrics — the wall-clock of its verify fan-out, the token usage summed across its
        per-proposition calls, and ``n_samples`` = the number of those calls (one verify call per
        proposition; a degraded/raised call still counts, contributing no usage).

        **Verifier failure degrades, never crashes (G1.17 R2).** If one verify call raises
        (endpoint down past retries, unparseable response, an enum that won't cast), that
        proposition keeps ``faithfulness`` *null* and its verdict slot is ``None`` so
        :meth:`_persist` logs the failure on the verify ``Action`` instead of letting an exception
        abort the whole document's batch. Its faithfulness being unassessed, the degraded
        proposition is folded to ``UNASSESSED_FAITHFULNESS`` (G1.21 — §3.1 D2: unassessed
        grounding is provisional, never coerced toward trusted), OR-folded onto any extract-time
        reason (a G1.14 twin keeps ``POLARITY_UNSTABLE`` too).
        """
        verifier = self.verifier
        assert verifier is not None  # only called when a verifier is configured

        async def verify_one(
            source: str, r: PropositionResult, parse_quality: float, usage_out: dict[str, int]
        ) -> tuple[PropositionResult, "_VerifyOut | None"]:
            try:
                async with sem:
                    verdict = await verifier.verify_proposition(source, r, usage_out=usage_out)
            except Exception as exc:
                # Degraded mode (R2): no verdict → faithfulness stays null. The proposition is
                # still persisted (the failure is recorded on the verify Action by _persist), but
                # its null faithfulness folds to UNASSESSED_FAITHFULNESS (G1.21) on top of any
                # extract-time reason — a G1.14 twin keeps POLARITY_UNSTABLE too.
                logger.warning(
                    "verifier unavailable for proposition %s (span %s): %s",
                    r.id,
                    r.span_id,
                    exc,
                )
                return _with_faithfulness_reason(r), None
            verify_component = faithfulness_from_verdict(
                verdict.entailment, verdict.polarity_preserved, verdict.modality_preserved
            )
            # Fold in the multi-sample agreement signal (G1.3) and the source parse-quality
            # penalty (G1.0). Both default to the 1.0 identity (single-pass N=1; digital/unknown
            # parse), so the common clean-text path is unchanged from G1.4/G1.5.
            agreement = r.agreement if r.agreement is not None else 1.0
            faith = combine_faithfulness(verify_component, agreement, parse_quality)
            # PropositionResult is frozen — rebuild with the score, then OR-fold the faithfulness
            # reason (R8 "never cleared"): _with_faithfulness_reason unions LOW_FAITHFULNESS below
            # the gate onto any extract-time reason, so a polarity-unstable twin (G1.14) stays
            # provisional even when the verifier finds it faithful.
            scored = _with_faithfulness_reason(replace(r, faithfulness=faith))
            return scored, verdict

        async def verify_group(
            i: int, results: list[PropositionResult]
        ) -> tuple[int, list[PropositionResult], list["_VerifyOut | None"], dict[str, Any]]:
            source = span_text(raw_text, spans[i])
            # The source span's parse-quality penalty (G1.0) — per span, shared by its
            # propositions; the worst region the span draws on governs (worst_source_quality).
            parse_quality = parse_quality_factor(worst_source_quality(spans[i].layout))
            # One usage slot per proposition's verify call (R12), folded into the span's metrics.
            usages: list[dict[str, int]] = [{} for _ in results]
            t0 = time.monotonic()
            pairs = await asyncio.gather(
                *(verify_one(source, r, parse_quality, usages[j]) for j, r in enumerate(results))
            )
            metrics = llm_metrics(
                duration_ms=elapsed_ms(t0), usages=usages, n_samples=len(results), cache_hit=False
            )
            return (
                i,
                [scored for scored, _ in pairs],
                [verdict for _, verdict in pairs],
                metrics,
            )

        return list(await asyncio.gather(*(verify_group(i, results) for i, results in inferred)))

    async def _extracted_hash(self, session: AsyncSession, span_id: uuid.UUID) -> str | None:
        """The content_hash of this span's most recent extraction, or ``None`` if never extracted.

        Action-table backed (single source of truth), mirroring ``ingest._segmented_hash``. The
        stored value drives the G1.7 idempotency decision in :meth:`propositionize_document`; it is
        ``None`` for a span with no prior extract Action (covers empty spans — they still record an
        Action) and for pre-G1.7 Actions that predate the ``content_hash`` input (none in practice).
        """
        row = await session.execute(
            text(
                "SELECT inputs->>'content_hash' FROM actions "
                "WHERE actor = 'propositionizer' AND inputs->>'target_span' = :sid "
                "ORDER BY timestamp DESC LIMIT 1"
            ),
            {"sid": str(span_id)},
        )
        return row.scalar_one_or_none()

    async def _has_proposition_dependents(self, session: AsyncSession, span_id: uuid.UUID) -> bool:
        """Whether any of a span's propositions has a graph edge **beyond** its ``EVIDENCED_BY`` →
        ``Span`` — i.e. a downstream (Phase-2+) node already derives from it (G1.7r).

        A clean Phase-1 ``Proposition`` has exactly one edge (its lone ``EVIDENCED_BY`` to the
        source span), so undirected **degree > 1** means an extra edge = a downstream consumer.
        Counting degree (not matching a specific edge type/direction) is robust to whatever
        relationship a later phase introduces. Used to refuse a cascade purge that would orphan
        such a consumer (:class:`CascadeDependentsError`) — the deferred full-cascade boundary.
        """
        from iknos.db.age import cypher_map, execute_cypher

        rows = await execute_cypher(
            session,
            f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(s:Span {cypher_map({'id': str(span_id)})}) "
            "OPTIONAL MATCH (p)-[r]-() "
            "WITH p, count(r) AS degree "
            "WHERE degree > 1 "
            "RETURN count(p)",
            returns="dependents agtype",
        )
        return bool(rows) and int(str(rows[0][0])) > 0

    async def _purge_span_propositions(
        self, session: AsyncSession, span_id: uuid.UUID
    ) -> list[str]:
        """Delete a span's existing propositions + their edges and index rows (cascade, G1.7r).

        No commit: runs in the **same transaction** as the re-persist that follows, so the
        purge and the re-write are atomic (a failure rolls back both, never half-purges a span).

        Removes the superseded **Phase-1** output across all three stores a proposition lands in
        (:meth:`_write_propositions`): the ``proposition_embeddings`` + lexical-index Postgres
        rows (by proposition id) and the ``Proposition`` vertices + their
        ``EVIDENCED_BY`` edges (AGE ``DETACH DELETE``). Caller guarantees the propositions have no
        downstream dependents (:meth:`_has_proposition_dependents`), so ``DETACH DELETE`` removes
        only the lone ``EVIDENCED_BY`` edge. Returns the purged proposition ids (for the
        re-extraction ``Action``'s audit trail — what was superseded).
        """
        from iknos.db.age import cypher_map, execute_cypher, unquote_agtype

        span_match = f"(s:Span {cypher_map({'id': str(span_id)})})"
        rows = await execute_cypher(
            session,
            f"MATCH (p:Proposition)-[:EVIDENCED_BY]->{span_match} RETURN p.id",
            returns="pid agtype",
        )
        purged = [unquote_agtype(r[0]) for r in rows]
        if not purged:
            return []
        prop_uuids = [uuid.UUID(pid) for pid in purged]
        # Postgres index rows first (no FK to the AGE graph, so order is for clarity not safety).
        await session.execute(
            text("DELETE FROM proposition_embeddings WHERE proposition_id = ANY(:ids)"),
            {"ids": prop_uuids},
        )
        await session.execute(
            text("DELETE FROM proposition_lexical_index WHERE proposition_id = ANY(:ids)"),
            {"ids": prop_uuids},
        )
        # The AGE vertices + their EVIDENCED_BY edges (DETACH DELETE drops the edge with the node).
        await execute_cypher(
            session,
            f"MATCH (p:Proposition)-[:EVIDENCED_BY]->{span_match} DETACH DELETE p",
        )
        return purged

    async def _guard_embedding_model(self, session: AsyncSession, document_id: uuid.UUID) -> None:
        """Refuse proposition vectors in a different embedding space than existing rows (G1.16).

        Unlike the span path, the extraction cache key (``extraction_content_hash``) keys on the
        *LLM extractor* model, not the embedding model — so swapping only the embedding substrate
        would slip past ``StaleExtractionError`` and silently mix two spaces in
        ``proposition_embeddings``. This is the load-bearing check that closes that hole. Checked
        once up front (fail fast, before any LLM inference); proposition vectors for a document are
        single-space by construction within a run.
        """
        row = await session.execute(
            text("SELECT model FROM proposition_embeddings WHERE document_id = :did LIMIT 1"),
            {"did": document_id},
        )
        existing = row.scalar_one_or_none()
        if existing is not None and existing != self.substrate.model_name:
            raise EmbeddingModelMismatchError(
                f"document {document_id} already has proposition vectors under embedding model "
                f"{existing!r}, cannot mix in {self.substrate.model_name!r}. Re-embed with "
                f"scripts/reembed.py to migrate the index first."
            )

    async def _write_propositions(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        results: list[PropositionResult],
    ) -> tuple[list[str], list[str]]:
        """Create each proposition vertex + EVIDENCED_BY edge + dense/lexical index rows.

        The shared write path for both fresh extraction (:meth:`_persist`) and G1.7b replay
        (:meth:`_persist_replay`): the two differ only in the ``Action``(s) they record, never in
        how a proposition lands — so the node/edge/index shape can never drift between them. Returns
        ``(prop_ids, edge_ids)`` for the recording Action's outputs. No commit (the caller owns the
        transaction boundary).
        """
        from iknos.db.age import cypher_map, execute_cypher

        prop_ids: list[str] = []
        edge_ids: list[str] = []
        for r in results:
            # Epistemic fields (§3.1) as vertex properties: StrEnums serialize to
            # plain strings, None -> null (faithfulness is a placeholder owned by G1.4/G1.5).
            # routing is the derived fact/judgement tag (G1.2).
            prop_props = {
                "id": str(r.id),
                "text": r.text,
                "polarity": r.polarity,
                "modality": r.modality,
                "attribution": r.attribution,
                "scope": r.scope,
                "epistemic_class": r.epistemic_class,
                "routing": r.routing,
                "faithfulness": r.faithfulness,
                # R8: the reason set (cypher_map JSON-encodes the list) is the source of truth;
                # the legacy boolean is written alongside for one transition release so pre-R8
                # readers (e.g. integration tests reading `p.provisional`) stay correct.
                # TODO(R8): drop `provisional` once every reader consumes provisional_reasons.
                "provisional_reasons": r.provisional_reasons,
                "provisional": legacy_provisional(r.faithfulness, r.provisional_reasons),
                # Multi-sample consistency (G1.3): null in single-pass mode, so this serializes
                # exactly as before until LLM_EXTRACT_SAMPLES is raised.
                "agreement": r.agreement,
            }
            await execute_cypher(
                session,
                f"CREATE (p:Proposition {cypher_map(prop_props)}) RETURN p",
                returns="p agtype",
            )
            await execute_cypher(
                session,
                f"MATCH (p:Proposition {cypher_map({'id': str(r.id)})}), "
                f"(s:Span {cypher_map({'id': str(r.span_id)})}) "
                "CREATE (p)-[e:EVIDENCED_BY]->(s) RETURN e",
                returns="e agtype",
            )
            session.add(
                PropositionEmbedding(
                    proposition_id=r.id,
                    document_id=document_id,
                    embedding=r.embedding,
                    # Vector-space identity (G1.16): proposition vectors come from
                    # substrate.embed_passages, so the embedding model is the substrate's.
                    model=self.substrate.model_name,
                )
            )
            await session.execute(
                text(
                    "INSERT INTO proposition_lexical_index (proposition_id, document_id, lexemes) "
                    "VALUES (:pid, :did, to_tsvector('simple', :txt))"
                ),
                {"pid": r.id, "did": document_id, "txt": r.text},
            )
            prop_ids.append(str(r.id))
            edge_ids.append(f"{r.id}->{r.span_id}")
        return prop_ids, edge_ids

    async def _build_replay_results(
        self, span: Span, reusable: ReusableExtraction, verify_sig: dict[str, Any] | None
    ) -> list[PropositionResult]:
        """Mint fresh :class:`PropositionResult`s from a cached extraction (G1.7b), for a new span.

        Each cached proposition becomes a new node: a new id, this span's provenance, the cached
        epistemic fields + agreement copied verbatim, and a **freshly computed** embedding. The
        vector is re-derived under the current substrate rather than copied from the source
        proposition because ``content_hash`` does *not* pin the embedding model (only the LLM
        extractor) — re-embedding keeps the ANN space single-model by construction (G1.16) and the
        local forward pass is negligible next to the LLM call this replay skips. Empty cache → no
        results (a cached empty extraction).

        **Faithfulness (G1.22).** The source's faithfulness is copied **only when the source's
        verify-stage identity matches the reusing run's current verifier** (``verify_sig``): a match
        means re-verifying would reproduce the same verdict, so the copy is sound. Otherwise the
        copied score is stale (a different/absent verifier now), so faithfulness is reset to null
        and the faithfulness reason re-derived to ``UNASSESSED_FAITHFULNESS``
        (:func:`reassess_faithfulness_reasons`); the verify-backfill pass then completes it under
        the current verifier (or it stays unassessed if there is none). Either way a quarantine is
        never silently dropped on replay.
        """
        if not reusable.propositions:
            return []
        # One batched forward pass off the event loop, like _infer_span's embed.
        vectors = await asyncio.to_thread(
            self.substrate.embed_passages, [c.text for c in reusable.propositions]
        )
        verify_matches = reusable.source_verify_sig == verify_sig
        results: list[PropositionResult] = []
        for c, v in zip(reusable.propositions, vectors, strict=True):
            if verify_matches:
                # Identity matches → trust the copied faithfulness; re-fold the faithfulness reason
                # (a copied null still lands UNASSESSED via G1.21).
                faithfulness = c.faithfulness
                reasons = c.provisional_reasons
            else:
                # Stale under the current verifier → unassess and queue for backfill.
                faithfulness = None
                reasons = reassess_faithfulness_reasons(c.provisional_reasons, None)
            results.append(
                _with_faithfulness_reason(
                    PropositionResult(
                        id=uuid.uuid4(),
                        text=c.text,
                        span_id=span.id,
                        document_id=span.document_id,
                        embedding=v,
                        polarity=c.polarity,
                        modality=c.modality,
                        attribution=c.attribution,
                        scope=c.scope,
                        epistemic_class=c.epistemic_class,
                        routing=c.routing,
                        faithfulness=faithfulness,
                        provisional_reasons=reasons,
                        agreement=c.agreement,
                    )
                )
            )
        return results

    async def _persist_replay(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        span_id: uuid.UUID,
        context_span_ids: list[str],
        content_hash: str,
        results: list[PropositionResult],
        reusable: ReusableExtraction,
        purge_existing: bool = False,
    ) -> uuid.UUID:
        """Persist a *replayed* span's propositions + edges + indexes + extract Action (G1.7b).

        The reuse twin of :meth:`_persist`: same node/edge/index writes (shared
        :meth:`_write_propositions`), but the extract ``Action`` carries a ``reused_from`` pointer
        instead of being a fresh LLM extraction, and **no verify Action is recorded here**: when
        the source's verify identity matched the current verifier the reused faithfulness is sound
        and ``reused_verify_sig`` records that (so the verify-backfill pass leaves the span alone);
        when it did not match, the caller already reset faithfulness to unassessed and the backfill
        pass verifies the span and records its own verify Action (G1.22). ``content_hash`` is still
        stored, so the *next* run sees this span as already-extracted (a true no-op). When
        ``purge_existing`` is set, the span's superseded propositions are purged in this txn first
        (cascade re-extraction whose replacement happens to be a reuse-replay, G1.7r). Returns the
        Action id.
        """
        superseded = await self._purge_span_propositions(session, span_id) if purge_existing else []
        prop_ids, edge_ids = await self._write_propositions(session, document_id, results)

        # Same sampling regime the cached extraction ran under (it is part of content_hash, so it
        # matches by construction) — recorded for parity with fresh extract Actions.
        extract_sampling: dict[str, object] = dict(self.sampling)
        if self.n_samples > 1:
            extract_sampling["n_samples"] = self.n_samples

        replay_outputs: dict[str, object] = {"propositions": prop_ids, "edges": edge_ids}
        if superseded:
            replay_outputs["superseded"] = superseded  # G1.7r cascade audit (as in _persist)
        replay_inputs: dict[str, Any] = {
            "target_span": str(span_id),
            "context_spans": context_span_ids,
            # G1.7 idempotency key — identical to a fresh extraction's, so a re-run no-ops.
            "content_hash": content_hash,
            "schema_version": EXTRACT_SCHEMA_VERSION,
            # G1.7b: these propositions were replayed from a prior identical-pipeline extraction
            # rather than re-running the LLM. The pointer keeps the audit chain — and the
            # original verify Action behind the reused faithfulness — one hop away.
            "reused_from": {
                "span": str(reusable.source_span_id),
                "action": str(reusable.source_action_id),
            },
        }
        # G1.22: this replay copied the source's faithfulness only because the source's verify-stage
        # identity matched the current verifier (the caller nulled it otherwise — see
        # _build_replay_results). Recording it lets _span_verify_identity treat the replayed span as
        # verified-under-this-sig without a verify Action, so the verify-backfill pass leaves it
        # alone (the spec's "replay copies faithfulness only when verify identity matches").
        if reusable.source_verify_sig is not None:
            replay_inputs["reused_verify_sig"] = reusable.source_verify_sig
        action_id = await record_action(
            session,
            actor="propositionizer",
            action_type="extract",
            inputs=replay_inputs,
            outputs=replay_outputs,
            model=self.llm.model,
            sampling=extract_sampling,
            # R12: a G1.7b replay paid no LLM — record cache_hit=True and omit token/n_samples/
            # duration (no sample was drawn here), so the §6.1 cost sum correctly attributes zero
            # extractor cost to a reuse while still counting it as an Action.
            metrics=llm_metrics(usages=[], cache_hit=True),
        )
        await session.commit()
        return action_id

    async def _persist(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        span_id: uuid.UUID,
        context_span_ids: list[str],
        content_hash: str,
        results: list[PropositionResult],
        verdicts: "list[_VerifyOut | None] | None" = None,
        twins: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
        extract_metrics: dict[str, Any] | None = None,
        verify_metrics: dict[str, Any] | None = None,
        purge_existing: bool = False,
    ) -> uuid.UUID:
        """Persist one span's propositions + edges + indexes + Action in a single transaction.

        When ``purge_existing`` is set (a cascade re-extraction, G1.7r), the span's superseded
        propositions are deleted **in this same transaction** before the new ones are written, so
        the swap is atomic. When verify verdicts are supplied (G1.4), a second Action (actor
        ``verifier``) records them in the *same* transaction — so a committed proposition's
        faithfulness always has an auditable verdict behind it. Returns the extract Action id.

        ``extract_metrics``/``verify_metrics`` are the R12 cost payloads (duration/usage/n_samples)
        built by :meth:`_infer_span` and :meth:`_verify_all` and recorded on the respective Actions.
        """
        superseded = await self._purge_span_propositions(session, span_id) if purge_existing else []
        prop_ids, edge_ids = await self._write_propositions(session, document_id, results)

        # Multi-sample audit (G1.3): record N in the sampling regime and the per-proposition
        # agreement, so the consistency signal is replayable and feeds Trial A5 straight from
        # actions.outputs. Single-pass (N=1) keeps the Action byte-identical to pre-G1.3.
        extract_outputs: dict[str, object] = {"propositions": prop_ids, "edges": edge_ids}
        # Cascade audit (G1.7r): the ids of the superseded propositions this re-extraction purged,
        # so "what replaced what" is reconstructable from the append-only Action log alone.
        if superseded:
            extract_outputs["superseded"] = superseded
        extract_sampling: dict[str, object] = dict(self.sampling)
        if self.n_samples > 1:
            extract_sampling["n_samples"] = self.n_samples
            extract_outputs["agreements"] = [
                {"proposition": str(r.id), "agreement": r.agreement} for r in results
            ]
            # Polarity twins (G1.14): the sampler wavered the sign of a claim — record the
            # pairing so Trial A5 can score it and the quarantine reason is auditable.
            if twins:
                extract_outputs["polarity_twins"] = [{"a": str(a), "b": str(b)} for a, b in twins]

        # R8: surface the quarantine reasons on the extract Action so triage (§11.1) and Trial A5
        # read *why* each atom was quarantined straight from actions.outputs. Omitted entirely when
        # nothing is provisional, so the clean-text path keeps the Action byte-identical.
        provisional_rows = [
            {"proposition": str(r.id), "reasons": r.provisional_reasons}
            for r in results
            if r.provisional_reasons
        ]
        if provisional_rows:
            extract_outputs["provisional"] = provisional_rows

        action_id = await record_action(
            session,
            actor="propositionizer",
            action_type="extract",
            inputs={
                "target_span": str(span_id),
                "context_spans": context_span_ids,
                # G1.7 content-addressed idempotency key — compared on the next run to decide
                # no-op vs re-extract vs StaleExtractionError. schema_version stored alongside so
                # a stale span is debuggable without recomputing the hash.
                "content_hash": content_hash,
                "schema_version": EXTRACT_SCHEMA_VERSION,
            },
            outputs=extract_outputs,
            model=self.llm.model,
            sampling=extract_sampling,
            metrics=extract_metrics,
        )

        # The verify pass is a distinct judgement by a distinct model (§13) — record it as
        # its own Action so faithfulness is auditable and the decomposed verdicts feed the
        # faithfulness-gate metric (Trial A5) straight from actions.outputs. Skip when there
        # are no propositions to verify (empty span).
        if verdicts:
            await self._record_verify_action(
                session, span_id, prop_ids, results, verdicts, metrics=verify_metrics
            )

        await session.commit()
        return action_id

    async def _record_verify_action(
        self,
        session: AsyncSession,
        span_id: uuid.UUID,
        prop_ids: list[str],
        results: list[PropositionResult],
        verdicts: "list[_VerifyOut | None]",
        *,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Record one span's verify ``Action`` — shared by inline verify and backfill (G1.22).

        The decomposed verdict rows are the faithfulness audit trail (Trial A5). A ``None`` verdict
        is a degraded-mode entry (G1.17 R2): the verifier was unavailable for that proposition, so
        its faithfulness stayed null and the failure is recorded rather than crashing the batch.

        The current :meth:`_verify_sig` is stamped on the Action **only when every proposition
        verified cleanly** — a degraded run (any verifier-unavailable proposition) omits it, so
        :meth:`_span_verify_identity` reports the span as not-yet-verified under this identity and a
        later run re-verifies it (verify-backfill retries transient verifier failures). No commit —
        the caller owns the transaction boundary.
        """
        assert self.verifier is not None
        verdict_rows: list[dict[str, Any]] = []
        for r, v in zip(results, verdicts, strict=True):
            if v is None:
                verdict_rows.append(
                    {
                        "proposition": str(r.id),
                        "verifier_unavailable": True,
                        "faithfulness": r.faithfulness,
                        "provisional_reasons": r.provisional_reasons,
                    }
                )
            else:
                verdict_rows.append(
                    {
                        "proposition": str(r.id),
                        "entailment": v.entailment,
                        "polarity_preserved": v.polarity_preserved,
                        "modality_preserved": v.modality_preserved,
                        "attribution_preserved": v.attribution_preserved,
                        "faithfulness": r.faithfulness,
                        "provisional_reasons": r.provisional_reasons,
                    }
                )
        inputs: dict[str, Any] = {"target_span": str(span_id), "propositions": prop_ids}
        if verdicts and all(v is not None for v in verdicts):
            inputs["verify_sig"] = self._verify_sig()
        await record_action(
            session,
            actor="verifier",
            action_type="verify",
            inputs=inputs,
            outputs={"verdicts": verdict_rows},
            model=self.verifier.llm.model,
            sampling=self.verifier.sampling,
            metrics=metrics,
        )

    def _verify_sig(self) -> dict[str, Any] | None:
        """The current verifier's stage identity, or ``None`` when no verifier is set (G1.22).

        ``{model, schema_version, prompt_sha, schema_sha}`` — model + the G1.15 rendered-prompt and
        schema digests, so a reworded or upgraded verifier has a *different* identity and reverifies
        (verify-backfill) rather than replaying a stale verdict. This is the **verification** stage
        key; it is deliberately *not* part of the extraction key (:meth:`_pipeline_hash`).
        """
        v = self.verifier
        if v is None:
            return None
        return {
            "model": v.llm.model,
            "schema_version": v.SCHEMA_VERSION,
            "prompt_sha": v.prompt_sha(),
            "schema_sha": v.schema_sha(),
        }

    async def _span_proposition_ids(self, session: AsyncSession, span_id: uuid.UUID) -> set[str]:
        """The ids of the propositions **currently** on a span — its current extraction generation.

        A cascade re-extraction (G1.7r) / replay purges the old propositions and writes a fresh set
        with new ids, so this set *is* the generation marker :meth:`_span_verify_identity` uses to
        reject a stale verify ``Action`` from a previous generation (G1.25).
        """
        from iknos.db.age import cypher_map, execute_cypher, unquote_agtype

        rows = await execute_cypher(
            session,
            f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(:Span {cypher_map({'id': str(span_id)})}) "
            "RETURN p.id",
            returns="pid agtype",
        )
        return {unquote_agtype(r[0]) for r in rows}

    async def _span_verify_identity(
        self, session: AsyncSession, span_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """The verify-stage identity a span's **current** propositions were verified under, or None.

        Source of truth, newest-first: a ``verify`` ``Action`` for the span records the
        ``verify_sig`` it ran under (only when the whole span verified cleanly; a degraded run omits
        it so the span re-verifies); a G1.7b **replay** instead records ``reused_verify_sig`` on its
        extract ``Action`` (it copied an already-verified faithfulness without re-running). ``None``
        means the current generation was never verified under any identity — the verify-backfill
        trigger. Compared to :meth:`_verify_sig` to decide if re-verification is owed (G1.22).

        **Generation-aware (G1.25).** ``Action``s are append-only and survive a cascade purge
        (:meth:`_purge_span_propositions`), so a verify ``Action`` can describe a *previous*
        extraction generation. It counts only if it verified the propositions that **currently**
        exist — its
        recorded ``propositions`` must intersect :meth:`_span_proposition_ids`. Because a
        re-extraction mints fresh ids, this is all-or-nothing per generation: a verify ``Action``
        whose ids are all gone verified a purged generation and must not vouch for the new one (the
        bug was a re-extract-with-verifier-off span staying ``UNASSESSED_FAITHFULNESS`` forever
        because run 1's clean verify ``Action`` kept matching). Node identity, not a timestamp
        comparison, because it encodes the invariant directly and is immune to same-transaction
        timestamp ties between an extract ``Action`` and its inline verify ``Action``. The
        ``reused_verify_sig`` path needs no such guard: it lives on the **newest** extract/replay
        ``Action``, which *defines* the current generation, so it can never be stale.
        """
        current_ids = await self._span_proposition_ids(session, span_id)
        # Select the whole ``inputs`` jsonb column (decodes to a Python dict) rather than an
        # ``inputs->'verify_sig'`` sub-expression, whose driver typing is unspecified — the same
        # discipline as ``reuse.find_reusable_extraction``.
        verify_row = await session.execute(
            text(
                "SELECT inputs FROM actions WHERE actor = 'verifier' "
                "AND inputs->>'target_span' = :sid ORDER BY timestamp DESC LIMIT 1"
            ),
            {"sid": str(span_id)},
        )
        verify_inputs = verify_row.scalar_one_or_none()
        if (
            verify_inputs is not None
            and verify_inputs.get("verify_sig") is not None
            # G1.25: only if this verify Action covers a *current* proposition (not a purged one).
            and current_ids.intersection(verify_inputs.get("propositions", []))
        ):
            verified: dict[str, Any] = verify_inputs["verify_sig"]
            return verified
        extract_row = await session.execute(
            text(
                "SELECT inputs FROM actions WHERE actor = 'propositionizer' "
                "AND inputs->>'target_span' = :sid ORDER BY timestamp DESC LIMIT 1"
            ),
            {"sid": str(span_id)},
        )
        extract_inputs = extract_row.scalar_one_or_none()
        if extract_inputs is not None and extract_inputs.get("reused_verify_sig") is not None:
            reused: dict[str, Any] = extract_inputs["reused_verify_sig"]
            return reused
        return None

    async def backfill_verification(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        spans: list[Span],
        raw_text: str,
    ) -> list[uuid.UUID]:
        """Verify already-extracted propositions whose verify identity is stale (G1.22 entrypoint).

        The decoupled verification stage. For each span whose propositions are **not** verified
        under the current verifier (:meth:`_verify_sig` vs :meth:`_span_verify_identity`), re-run
        the verifier over the *existing* propositions, recompute faithfulness from the stored
        agreement, update ``faithfulness``/``provisional``(+reasons) in place, and record a verify
        ``Action`` — **without re-running the extractor or purging anything** (the extractor's
        output does not depend on the verifier, so a verifier toggle/upgrade is a cheap re-verify,
        not a re-extraction). A no-op when no verifier is set (nothing can be assessed) or every
        span is already verified under the current identity. Per-span transaction isolation
        (G1.17 R1): one span's failure rolls back only its own update. Returns the backfilled span
        ids. Called at the tail of :meth:`propositionize_document`, and usable alone to complete
        faithfulness when a verifier is first enabled on a previously verifier-off corpus.
        """
        if self.verifier is None:
            return []
        sig = self._verify_sig()
        backfilled: list[uuid.UUID] = []
        for span in spans:
            if await self._span_verify_identity(session, span.id) == sig:
                continue  # already verified under the current identity — true no-op.
            try:
                if await self._verify_backfill_span(session, span, raw_text):
                    backfilled.append(span.id)
            except Exception:  # noqa: BLE001 — isolate; roll back this span and continue
                await session.rollback()
                logger.exception("verify-backfill failed for span %s; isolating", span.id)
        return backfilled

    async def _verify_backfill_span(self, session: AsyncSession, span: Span, raw_text: str) -> bool:
        """Re-verify one span's existing propositions and update them in place (G1.22).

        Loads the committed propositions, runs the verifier over each (degrading per-prop like
        the inline pass, G1.17 R2 — a verifier failure leaves that atom unassessed rather than
        crashing the batch), recomputes faithfulness from the *stored* agreement and the span's
        parse-quality, rewrites ``faithfulness``/``provisional``(+reasons) on the node, and records
        a verify ``Action`` in the same transaction. ``False`` (no commit) when the span has no
        propositions (an empty extraction — nothing to verify). The faithfulness leg is
        *re-derived* (:func:`reassess_faithfulness_reasons`), so a prior ``UNASSESSED_FAITHFULNESS``
        is replaced by the assessed result while an independent twin reason survives.
        """
        assert self.verifier is not None
        verifier = self.verifier
        props = await self._load_span_propositions(session, span)
        if not props:
            return False
        source = span_text(raw_text, span)
        parse_quality = parse_quality_factor(worst_source_quality(span.layout))
        sem = asyncio.Semaphore(self.concurrency)

        async def verify_one(
            r: PropositionResult, usage_out: dict[str, int]
        ) -> tuple[PropositionResult, "_VerifyOut | None"]:
            try:
                async with sem:
                    verdict = await verifier.verify_proposition(source, r, usage_out=usage_out)
            except Exception as exc:  # noqa: BLE001 — degrade (R2), do not crash the backfill
                logger.warning(
                    "verifier unavailable for proposition %s (span %s): %s", r.id, span.id, exc
                )
                degraded = replace(
                    r,
                    faithfulness=None,
                    provisional_reasons=reassess_faithfulness_reasons(r.provisional_reasons, None),
                )
                return degraded, None
            verify_component = faithfulness_from_verdict(
                verdict.entailment, verdict.polarity_preserved, verdict.modality_preserved
            )
            agreement = r.agreement if r.agreement is not None else 1.0
            faith = combine_faithfulness(verify_component, agreement, parse_quality)
            scored = replace(
                r,
                faithfulness=faith,
                provisional_reasons=reassess_faithfulness_reasons(r.provisional_reasons, faith),
            )
            return scored, verdict

        # One usage slot per re-verify call (R12), folded into this verify Action's metrics.
        usages: list[dict[str, int]] = [{} for _ in props]
        t0 = time.monotonic()
        pairs = await asyncio.gather(*(verify_one(r, usages[j]) for j, r in enumerate(props)))
        verify_metrics = llm_metrics(
            duration_ms=elapsed_ms(t0), usages=usages, n_samples=len(props), cache_hit=False
        )
        scored = [s for s, _ in pairs]
        verdicts = [v for _, v in pairs]
        for r in scored:
            await self._update_proposition_faithfulness(session, r)
        await self._record_verify_action(
            session, span.id, [str(r.id) for r in scored], scored, verdicts, metrics=verify_metrics
        )
        await session.commit()
        return True

    async def _load_span_propositions(
        self, session: AsyncSession, span: Span
    ) -> list[PropositionResult]:
        """Read a span's committed propositions as :class:`PropositionResult`s (G1.22 backfill).

        Only the fields verification + the in-place update need: the epistemic operators and text
        (for the verifier), the stored ``agreement`` and existing ``provisional_reasons`` (to
        recompute and preserve non-faithfulness reasons). The embedding is **not** read — backfill
        rewrites faithfulness, never the vector — so it is left empty; these results are never
        persisted through :meth:`_write_propositions`.
        """
        from iknos.db.age import cypher_map, execute_cypher, parse_agtype_map, unquote_agtype

        rows = await execute_cypher(
            session,
            f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(s:Span {cypher_map({'id': str(span.id)})}) "
            "RETURN p.id, properties(p)",
            returns="id agtype, props agtype",
        )
        results: list[PropositionResult] = []
        for rid, raw in rows:
            props = parse_agtype_map(raw)
            results.append(
                PropositionResult(
                    id=uuid.UUID(unquote_agtype(rid)),
                    text=props["text"],
                    span_id=span.id,
                    document_id=span.document_id,
                    embedding=[],
                    polarity=Polarity(props["polarity"]),
                    modality=Modality(props["modality"]),
                    attribution=Attribution(props["attribution"]),
                    scope=props.get("scope", ""),
                    epistemic_class=EpistemicClass(props["epistemic_class"]),
                    routing=Routing(props["routing"]),
                    faithfulness=props.get("faithfulness"),
                    provisional_reasons=decode_provisional_reasons(
                        props.get("provisional_reasons")
                    ),
                    agreement=props.get("agreement"),
                )
            )
        return results

    async def _update_proposition_faithfulness(
        self, session: AsyncSession, r: PropositionResult
    ) -> None:
        """Rewrite a proposition node's faithfulness/provisional fields in place (G1.22 backfill).

        A targeted per-property ``SET`` over the three faithfulness-derived properties only — the
        epistemic fields, text, and indexes are untouched (the extractor output did not change, only
        its grounding assessment). Mirrors the encoding of :meth:`_write_propositions` and the
        in-place update in ``core/reference.py`` (the reason list is a JSON string; the legacy
        boolean tracks non-emptiness; ``faithfulness``/``provisional`` ``null`` when unassessed).
        """
        from iknos.db.age import cypher_map, cypher_string_literal, execute_cypher

        # json.dumps renders Cypher-valid scalar literals: a float, or `null`/`true`/`false`.
        faith_lit = json.dumps(r.faithfulness)
        provisional = legacy_provisional(r.faithfulness, r.provisional_reasons)
        provisional_lit = json.dumps(provisional)
        reasons_lit = cypher_string_literal(json.dumps(r.provisional_reasons))
        await execute_cypher(
            session,
            f"MATCH (p:Proposition {cypher_map({'id': str(r.id)})}) "
            f"SET p.faithfulness = {faith_lit}, p.provisional_reasons = {reasons_lit}, "
            f"p.provisional = {provisional_lit}",
        )

    def _pipeline_hash(self, spans: list[Span], index: int, raw_text: str) -> str:
        """The G1.7 content-addressed idempotency key for one span's extraction (core/cache.py).

        Pure (no DB): the target text, the preceding-window context the extractor actually sees
        (its text *and* the ordered ids of the spans that produced it, G1.24), and the full
        extractor identity — model, schema version, sampling regime (incl. n_samples). Two runs
        that would produce the same extraction share a key.

        The verifier is **not** in this key (G1.22): the extractor's output is independent of it, so
        a verifier toggle/upgrade drives verify-backfill, not re-extraction
        (:meth:`_span_verify_identity` keys the verify stage separately).
        """
        context_spans, context_text = build_context(spans, index, raw_text, self.context_window)
        regime = {**self.sampling, "n_samples": self.n_samples}
        return extraction_content_hash(
            target_text=span_text(raw_text, spans[index]),
            context_text=context_text,
            # G1.24: which spans (ordered) front the window, so a re-segmentation that changes the
            # K-span context set re-keys even when the rendered text looks similar. Trade-off
            # (decided): span ids are uuid5(document_id, …) (ingest.span_id_for), so a non-empty
            # context window makes the hash document-specific — a span with context never reuses
            # (G1.7b) across documents, only first/single-span (empty-context) spans do. The price
            # of keying on ingest identity, not textual coincidence; see core/reuse.py.
            context_span_ids=[str(s.id) for s in context_spans],
            model=self.llm.model,
            schema_version=EXTRACT_SCHEMA_VERSION,
            # G1.15: the rendered prompt + schema themselves, so a reworded prompt re-extracts
            # without a manual EXTRACT_SCHEMA_VERSION bump.
            prompt_sha=EXTRACTOR_PROMPT_SHA,
            schema_sha=EXTRACTOR_SCHEMA_SHA,
            sampling=regime,
        )

    async def propositionize_document(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        spans: list[Span],
        raw_text: str,
    ) -> PropositionizeReport:
        """Run the full pipeline for one document.

        Returns a :class:`PropositionizeReport` — the extract Action ids committed this run plus
        any spans that failed *in isolation* (G1.17 R1): one span's extraction or persistence
        raising no longer aborts the whole document. A failed span records no Action, so a re-run
        re-extracts exactly it via the content-addressed idempotency check (Phase 1) — resume is
        free. Whole-document contract violations (an embedding-model swap, a stale pipeline) stay
        fail-loud below; only per-span *runtime* failures are isolated.
        """
        # G1.16: fail fast before any LLM inference if this document already has proposition
        # vectors from a different embedding model — never mix two ANN spaces.
        await self._guard_embedding_model(session, document_id)

        # Phase 1: version-aware idempotency (G1.7), serial reads on the shared session. Each
        # span's pipeline content hash is compared against the one stored on its prior extract
        # Action: identical → skip (true no-op); different → the extractor model/prompt/regime/
        # context changed since it was extracted, so fail loud rather than orphan the old
        # propositions (cascade re-extract is still future); absent → never extracted, so either
        # *replay* a prior identical-pipeline extraction (G1.7b cross-doc reuse, below) or run the
        # LLM. The verifier is NOT in this key (G1.22) — a verifier change drives the separate
        # verify-backfill pass below, not re-extraction. The hash is computed once here and reused
        # at persist time so the stored key can never drift from the decision it drove.
        verify_sig = self._verify_sig()
        pending: list[int] = []
        to_replay: list[tuple[int, ReusableExtraction]] = []
        hash_by_index: dict[int, str] = {}
        stale: set[int] = set()  # indices whose superseded propositions must be purged (G1.7r)
        for i, s in enumerate(spans):
            chash = self._pipeline_hash(spans, i, raw_text)
            hash_by_index[i] = chash
            stored = await self._extracted_hash(session, s.id)
            if stored == chash:
                continue  # already extracted with this exact pipeline: a true no-op.
            if stored is not None:
                # A different pipeline than last time. G1.7r: cascade re-extract — purge the
                # superseded propositions and re-derive — unless cascade is disabled (fail loud) or
                # the span's propositions already feed downstream nodes (refuse — the deferred full
                # cascade). Once cleared, a stale span is handled exactly like a never-extracted one
                # (reuse-or-infer below); only the purge-before-persist differs.
                if not self.cascade_reextract:
                    raise StaleExtractionError(
                        f"span {s.id} was extracted under a different pipeline "
                        f"(stored {stored[:12]}…, now {chash[:12]}…) and cascade re-extraction is "
                        f"disabled."
                    )
                if await self._has_proposition_dependents(session, s.id):
                    raise CascadeDependentsError(
                        f"span {s.id}'s propositions feed downstream nodes; cascade re-extraction "
                        f"would orphan them (the full downstream cascade is deferred)."
                    )
                stale.add(i)
            # Never extracted (or a cleared-to-cascade stale span). G1.7b: if an identical-pipeline
            # extraction exists
            # anywhere (same content_hash — same target text, context, model, prompt/schema, regime,
            # verifier), replay its propositions into this span instead of paying the LLM again.
            # Each replayed span still records its own extract Action, so the per-span idempotency
            # key (and the soundness reason it is per-span, not pure-content) is preserved.
            reusable = (
                await find_reusable_extraction(session, chash) if self.reuse_extractions else None
            )
            if reusable is not None:
                to_replay.append((i, reusable))
            else:
                pending.append(i)

        # Phase 2: concurrent inference, bounded by a semaphore, with no DB access. The permit is
        # acquired *inside* _infer_span around each individual sample call (G1.3 fans out N per
        # span), so this coroutine must not hold one itself — that would deadlock at low
        # concurrency. Same permit discipline as the verify fan-out below.
        sem = asyncio.Semaphore(self.concurrency)

        # Per-span error isolation (G1.17 R1): each span's inference is wrapped so one flaky
        # span (or one flaky sample within it) cannot abort the document. A failure is captured
        # with its index and tagged onto the run report; the span is dropped from the downstream
        # verify/persist phases and re-extracted on the next run (it recorded no Action).
        failed_spans: list[FailedSpan] = []

        async def infer(
            i: int,
        ) -> tuple[
            int,
            list[PropositionResult],
            list[tuple[uuid.UUID, uuid.UUID]],
            dict[str, Any],
            BaseException | None,
        ]:
            try:
                results, twins, metrics = await self._infer_span(sem, spans, i, raw_text)
                return i, results, twins, metrics, None
            except Exception as exc:  # noqa: BLE001 — isolate; the span re-runs via idempotency
                logger.exception("extraction failed for span %s; isolating", spans[i].id)
                return i, [], [], {}, exc

        inferred_raw = await asyncio.gather(*(infer(i) for i in pending))
        ok_raw: list[
            tuple[int, list[PropositionResult], list[tuple[uuid.UUID, uuid.UUID]], dict[str, Any]]
        ] = []
        for i, results, twins, metrics, exc in inferred_raw:
            if exc is not None:
                failed_spans.append(FailedSpan(span_id=spans[i].id, phase="infer", error=str(exc)))
            else:
                ok_raw.append((i, results, twins, metrics))

        # Twins and the R12 extract metrics ride alongside the per-span hash for persistence; the
        # verify fan-out below operates only on (index, results), so split them out here (G1.14).
        twins_by_index: dict[int, list[tuple[uuid.UUID, uuid.UUID]]] = {
            i: twins for i, _, twins, _ in ok_raw
        }
        extract_metrics_by_index: dict[int, dict[str, Any]] = {
            i: metrics for i, _, _, metrics in ok_raw
        }
        inferred = [(i, results) for i, results, _, _ in ok_raw]

        # Phase 2-replay (G1.7b): build replay results for the reusable spans — re-embed the cached
        # proposition text under the current substrate, no LLM and no verify (the reused
        # faithfulness was already computed on the source). DB-free, so it runs in the concurrent
        # phase; per-span isolated like inference (a failed replay records no Action → re-runs next
        # time, re-attempting reuse or falling through to a fresh extraction).
        async def replay(
            i: int, reusable: ReusableExtraction
        ) -> tuple[int, list[PropositionResult], ReusableExtraction, BaseException | None]:
            try:
                results = await self._build_replay_results(spans[i], reusable, verify_sig)
                return i, results, reusable, None
            except Exception as exc:  # noqa: BLE001 — isolate; the span re-runs next time
                logger.exception("replay failed for span %s; isolating", spans[i].id)
                return i, [], reusable, exc

        replayed_raw = await asyncio.gather(*(replay(i, ru) for i, ru in to_replay))
        replayed: list[tuple[int, list[PropositionResult], ReusableExtraction]] = []
        for i, results, reusable, exc in replayed_raw:
            if exc is not None:
                failed_spans.append(FailedSpan(span_id=spans[i].id, phase="replay", error=str(exc)))
            else:
                replayed.append((i, results, reusable))

        # Phase 2b: independent verification (G1.4) — another DB-free LLM call, so it runs
        # in the concurrent phase under the same budget. Absent verifier → verdicts stay None and
        # faithfulness stays null; that null grounding folds to UNASSESSED_FAITHFULNESS (G1.21 —
        # §3.1 D2: unassessed is provisional, never coerced toward trusted), so a verifier-off
        # ingest quarantines its atoms until G1.22 backfill completes their faithfulness.
        verified: list[tuple[int, list[PropositionResult], list[_VerifyOut | None] | None]]
        # R12 verify metrics ride a side dict keyed by span index, like the extract metrics: the
        # verifier-off path records no verify Action, so it contributes nothing here.
        verify_metrics_by_index: dict[int, dict[str, Any]] = {}
        if self.verifier is None:
            verified = [
                (i, [_with_faithfulness_reason(r) for r in results], None)
                for i, results in inferred
            ]
        else:
            verified = []
            for i, results, vd, vmetrics in await self._verify_all(sem, spans, raw_text, inferred):
                verified.append((i, results, vd))
                verify_metrics_by_index[i] = vmetrics

        # Phase 3: serial persistence — one short transaction per span. Per-span isolation again
        # (G1.17 R1): a write failure on one span rolls back *its* transaction and is recorded,
        # leaving prior spans committed and later spans to proceed; the failed span re-runs next
        # time (no Action committed for it). A poisoned-session abort of the whole batch is exactly
        # what this prevents.
        action_ids: list[uuid.UUID] = []
        for i, results, verdicts in verified:
            context_spans, _ = build_context(spans, i, raw_text, self.context_window)
            context_ids = [str(s.id) for s in context_spans]
            try:
                action_ids.append(
                    await self._persist(
                        session,
                        document_id,
                        spans[i].id,
                        context_ids,
                        hash_by_index[i],
                        results,
                        verdicts,
                        twins_by_index.get(i),
                        extract_metrics=extract_metrics_by_index.get(i),
                        verify_metrics=verify_metrics_by_index.get(i),
                        purge_existing=i in stale,  # G1.7r cascade: purge superseded props first
                    )
                )
            except Exception as exc:  # noqa: BLE001 — isolate; roll back this span and continue
                await session.rollback()
                logger.exception("persist failed for span %s; isolating", spans[i].id)
                failed_spans.append(
                    FailedSpan(span_id=spans[i].id, phase="persist", error=str(exc))
                )

        # Phase 3-replay (G1.7b): persist the replayed spans, same per-span isolation. Each records
        # an extract Action with a reused_from pointer and no verify Action here (when the source's
        # verify identity matched, the copied faithfulness is sound; otherwise the Phase 4 backfill
        # below verifies it) — same content_hash, so a re-run no-ops.
        for i, results, reusable in replayed:
            context_spans, _ = build_context(spans, i, raw_text, self.context_window)
            context_ids = [str(s.id) for s in context_spans]
            try:
                action_ids.append(
                    await self._persist_replay(
                        session,
                        document_id,
                        spans[i].id,
                        context_ids,
                        hash_by_index[i],
                        results,
                        reusable,
                        purge_existing=i in stale,  # G1.7r cascade: purge superseded props first
                    )
                )
            except Exception as exc:  # noqa: BLE001 — isolate; roll back this span and continue
                await session.rollback()
                logger.exception("replay persist failed for span %s; isolating", spans[i].id)
                failed_spans.append(
                    FailedSpan(span_id=spans[i].id, phase="persist", error=str(exc))
                )

        # Phase 4: verify-backfill (G1.22) — the decoupled verification stage. A no-op span (already
        # extracted) whose verifier was since enabled/upgraded, or a replay whose source verify
        # identity did not match, is verified now over its existing propositions: faithfulness is
        # completed in place with zero extractor calls and zero purges. Freshly-verified and
        # identity-matched spans are skipped (already verified under this identity). Verifier-off →
        # no-op (those atoms stay UNASSESSED until a verifier is configured, G1.21).
        await self.backfill_verification(session, document_id, spans, raw_text)

        return PropositionizeReport(action_ids=action_ids, failed_spans=failed_spans)
