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
import logging
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
)
from iknos.core.embeddings import EmbeddingModelMismatchError, EmbeddingSubstrate
from iknos.core.llm import LLMClient
from iknos.core.parse import parse_quality_factor, worst_source_quality
from iknos.core.prompts import vocab
from iknos.core.reuse import ReusableExtraction, find_reusable_extraction
from iknos.db.orm import PropositionEmbedding
from iknos.provenance.action_log import record_action
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    ProvisionalReason,
    Routing,
    combine_faithfulness,
    faithfulness_from_verdict,
    legacy_provisional,
    merge_provisional_reasons,
    provisional_reasons_for,
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
    model, prompt/schema version, sampling regime, verifier signature and context (core/cache.py).
    An identical re-run is a true no-op; a span whose stored hash differs from the current one
    means the model/prompt/regime/verifier changed since it was extracted. Re-extracting in place
    would leave the old propositions orphaned alongside the new ones, so until cascade
    re-extraction lands (G1.7b) this fails loud — mirrors
    ``core/ingest.py::DocumentResegmentationError``.
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
        substrate: EmbeddingSubstrate,
        *,
        context_window: int = 8,
        concurrency: int = 8,
        sampling: dict[str, object] | None = None,
        verifier: "Verifier | None" = None,
        n_samples: int = 1,
        agreement_threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
        reuse_extractions: bool = True,
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
        # Optional independent verifier (G1.4). Absent → faithfulness/provisional stay null
        # (the documented G1.1 state); production wires it from settings when configured.
        self.verifier = verifier
        # Multi-sample extraction (G1.3): sample the extractor n_samples times and score each
        # proposition by cross-sample agreement. n_samples=1 is a strict no-op (no clustering,
        # agreement null) — byte-identical to single-pass. n_samples>1 with a greedy regime is a
        # misconfiguration (the N samples would be identical → no signal), so fail loud.
        if n_samples < 1:
            raise ValueError(f"n_samples must be >= 1, got {n_samples!r}")
        if (
            n_samples > 1
            and self.sampling.get("temperature", 0) == 0
            and "top_p" not in self.sampling
        ):
            raise ValueError(
                f"multi-sample extraction (n_samples={n_samples}) needs a temperature>0 sampling "
                f"regime; got greedy sampling {self.sampling!r}"
            )
        self.n_samples = n_samples
        self.agreement_threshold = agreement_threshold

    async def _infer_span(
        self, sem: asyncio.Semaphore, spans: list[Span], index: int, raw_text: str
    ) -> tuple[list[PropositionResult], list[tuple[uuid.UUID, uuid.UUID]]]:
        """Extract one span's propositions, scoring each by cross-sample agreement (G1.3/G1.14).

        Samples the extractor ``n_samples`` times, embeds every candidate, then consolidates
        semantically-equivalent extractions **within each ``(polarity, epistemic_class)``
        partition** (G1.14 — cosine cannot tell a claim from its negation); each cluster becomes
        one proposition carrying its medoid's text+operators and the fraction of samples that
        produced it (``agreement``). No DB access — concurrent-phase safe. The semaphore wraps each
        *individual* sample call (not the whole span), so the N samples of one span share the global
        budget with every other in-flight call and never deadlock by holding an outer permit while
        awaiting inner ones — the same permit discipline as ``_verify_all``.

        Returns ``(results, twin_pairs)`` where ``twin_pairs`` are ``(id, id)`` proposition pairs
        the sampler wavered the *sign* of (polarity twins, G1.14) — both flagged ``provisional`` and
        recorded on the extract ``Action``. n_samples=1 short-circuits the clustering (1:1
        candidate→proposition, ``agreement`` null, no twins), so single-pass behavior is
        byte-identical to pre-G1.3.
        """
        target = spans[index]
        _, context_text = build_context(spans, index, raw_text, self.context_window)
        messages = build_messages(context_text, span_text(raw_text, target))

        async def sample_once() -> list[_PropositionOut]:
            async with sem:
                raw = await self.llm.guided_complete(messages, EXTRACTION_SCHEMA, self.sampling)
            return PropositionExtraction.model_validate(raw).propositions

        samples = await asyncio.gather(*(sample_once() for _ in range(self.n_samples)))

        # Flatten the N samples into candidates, preserving (sample_index, position) so the
        # downstream clustering/medoid order is deterministic.
        flat = [
            (s_idx, pos, p) for s_idx, props in enumerate(samples) for pos, p in enumerate(props)
        ]
        if not flat:
            return [], []

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
            return results, []

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
        return results, twin_pairs

    async def _verify_all(
        self,
        sem: asyncio.Semaphore,
        spans: list[Span],
        raw_text: str,
        inferred: list[tuple[int, list[PropositionResult]]],
    ) -> list[tuple[int, list[PropositionResult], list["_VerifyOut | None"]]]:
        """Verify each inferred proposition against its source span (G1.4) and attach the
        derived faithfulness/provisional (G1.5).

        A separate concurrent fan-out, run after extraction completes and bounded by the
        *same* semaphore — each verify call acquires its own permit, so it never nests
        inside an extract permit (which would serialize throughput). Returns the results
        re-scored, paired with the raw verdicts for the verify Action.

        **Verifier failure degrades, never crashes (G1.17 R2).** If one verify call raises
        (endpoint down past retries, unparseable response, an enum that won't cast), that
        proposition keeps ``faithfulness``/``provisional`` *null* — the documented degraded G1.1
        mode — and its verdict slot is ``None`` so :meth:`_persist` logs the failure on the verify
        ``Action`` instead of letting an exception abort the whole document's batch.
        """
        verifier = self.verifier
        assert verifier is not None  # only called when a verifier is configured

        async def verify_one(
            source: str, r: PropositionResult, parse_quality: float
        ) -> tuple[PropositionResult, "_VerifyOut | None"]:
            try:
                async with sem:
                    verdict = await verifier.verify_proposition(source, r)
            except Exception as exc:
                # Degraded mode (R2): no verdict → faithfulness/provisional stay as inferred
                # (null, or provisional=True for a G1.14 twin). The proposition is still
                # persisted; the failure is recorded on the verify Action by _persist.
                logger.warning(
                    "verifier unavailable for proposition %s (span %s): %s",
                    r.id,
                    r.span_id,
                    exc,
                )
                return r, None
            verify_component = faithfulness_from_verdict(
                verdict.entailment, verdict.polarity_preserved, verdict.modality_preserved
            )
            # Fold in the multi-sample agreement signal (G1.3) and the source parse-quality
            # penalty (G1.0). Both default to the 1.0 identity (single-pass N=1; digital/unknown
            # parse), so the common clean-text path is unchanged from G1.4/G1.5.
            agreement = r.agreement if r.agreement is not None else 1.0
            faith = combine_faithfulness(verify_component, agreement, parse_quality)
            # OR-fold (R8 "never cleared"): union the faithfulness-derived reason onto any
            # already present — a polarity-unstable twin (G1.14) stays provisional even if the
            # verifier finds it faithful, the instability being an independent quarantine reason.
            reasons = merge_provisional_reasons(
                r.provisional_reasons, provisional_reasons_for(faith)
            )
            # PropositionResult is frozen — rebuild with the scored fields, never mutate.
            scored = replace(r, faithfulness=faith, provisional_reasons=reasons)
            return scored, verdict

        async def verify_group(
            i: int, results: list[PropositionResult]
        ) -> tuple[int, list[PropositionResult], list["_VerifyOut | None"]]:
            source = span_text(raw_text, spans[i])
            # The source span's parse-quality penalty (G1.0) — per span, shared by its
            # propositions; the worst region the span draws on governs (worst_source_quality).
            parse_quality = parse_quality_factor(worst_source_quality(spans[i].layout))
            pairs = await asyncio.gather(*(verify_one(source, r, parse_quality) for r in results))
            return i, [scored for scored, _ in pairs], [verdict for _, verdict in pairs]

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
        self, span: Span, reusable: ReusableExtraction
    ) -> list[PropositionResult]:
        """Mint fresh :class:`PropositionResult`s from a cached extraction (G1.7b), for a new span.

        Each cached proposition becomes a new node: a new id, this span's provenance, the cached
        epistemic fields + faithfulness/agreement copied verbatim, and a **freshly computed**
        embedding. The vector is re-derived under the current substrate rather than copied from the
        source proposition because ``content_hash`` does *not* pin the embedding model (only the LLM
        extractor) — re-embedding keeps the ANN space single-model by construction (G1.16) and the
        local forward pass is negligible next to the LLM call this replay skips. Empty cache → no
        results (a cached empty extraction).
        """
        if not reusable.propositions:
            return []
        # One batched forward pass off the event loop, like _infer_span's embed.
        vectors = await asyncio.to_thread(
            self.substrate.embed_passages, [c.text for c in reusable.propositions]
        )
        return [
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
                faithfulness=c.faithfulness,
                provisional_reasons=c.provisional_reasons,
                agreement=c.agreement,
            )
            for c, v in zip(reusable.propositions, vectors, strict=True)
        ]

    async def _persist_replay(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        span_id: uuid.UUID,
        context_span_ids: list[str],
        content_hash: str,
        results: list[PropositionResult],
        reusable: ReusableExtraction,
    ) -> uuid.UUID:
        """Persist a *replayed* span's propositions + edges + indexes + extract Action (G1.7b).

        The reuse twin of :meth:`_persist`: same node/edge/index writes (shared
        :meth:`_write_propositions`), but the extract ``Action`` carries a ``reused_from`` pointer
        instead of being a fresh LLM extraction, and **no verify Action is recorded** — the reused
        ``faithfulness``/``provisional`` were already verified on the source proposition (the
        verifier signature is in ``content_hash``), and the pointer keeps that original verify
        ``Action`` one hop away. ``content_hash`` is still stored, so the *next* run sees this span
        as already-extracted (a true no-op), exactly like a fresh extraction. Returns the Action id.
        """
        prop_ids, edge_ids = await self._write_propositions(session, document_id, results)

        # Same sampling regime the cached extraction ran under (it is part of content_hash, so it
        # matches by construction) — recorded for parity with fresh extract Actions.
        extract_sampling: dict[str, object] = dict(self.sampling)
        if self.n_samples > 1:
            extract_sampling["n_samples"] = self.n_samples

        action_id = await record_action(
            session,
            actor="propositionizer",
            action_type="extract",
            inputs={
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
            },
            outputs={"propositions": prop_ids, "edges": edge_ids},
            model=self.llm.model,
            sampling=extract_sampling,
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
    ) -> uuid.UUID:
        """Persist one span's propositions + edges + indexes + Action in a single transaction.

        When verify verdicts are supplied (G1.4), a second Action (actor ``verifier``) records
        them in the *same* transaction — so a committed proposition's faithfulness always has
        an auditable verdict behind it. Returns the extract Action id.
        """
        prop_ids, edge_ids = await self._write_propositions(session, document_id, results)

        # Multi-sample audit (G1.3): record N in the sampling regime and the per-proposition
        # agreement, so the consistency signal is replayable and feeds Trial A5 straight from
        # actions.outputs. Single-pass (N=1) keeps the Action byte-identical to pre-G1.3.
        extract_outputs: dict[str, object] = {"propositions": prop_ids, "edges": edge_ids}
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
        )

        # The verify pass is a distinct judgement by a distinct model (§13) — record it as
        # its own Action so faithfulness is auditable and the decomposed verdicts feed the
        # faithfulness-gate metric (Trial A5) straight from actions.outputs. Skip when there
        # are no propositions to verify (empty span).
        if verdicts:
            assert self.verifier is not None
            # A None verdict is a degraded-mode entry (G1.17 R2): the verifier was unavailable for
            # that proposition, so faithfulness/provisional stayed null and the failure is recorded
            # here (rather than crashing the batch) — the Action stays the faithfulness audit trail.
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
            await record_action(
                session,
                actor="verifier",
                action_type="verify",
                inputs={"target_span": str(span_id), "propositions": prop_ids},
                outputs={"verdicts": verdict_rows},
                model=self.verifier.llm.model,
                sampling=self.verifier.sampling,
            )

        await session.commit()
        return action_id

    def _pipeline_hash(
        self, spans: list[Span], index: int, raw_text: str, verifier_sig: dict[str, Any] | None
    ) -> str:
        """The G1.7 content-addressed idempotency key for one span's extraction (core/cache.py).

        Pure (no DB): the target text, the preceding-window context the extractor actually sees,
        and the full pipeline identity — model, schema version, sampling regime (incl. n_samples),
        and verifier signature. Two runs that would produce the same extraction share a key.
        """
        _, context_text = build_context(spans, index, raw_text, self.context_window)
        regime = {**self.sampling, "n_samples": self.n_samples}
        return extraction_content_hash(
            target_text=span_text(raw_text, spans[index]),
            context_text=context_text,
            model=self.llm.model,
            schema_version=EXTRACT_SCHEMA_VERSION,
            # G1.15: the rendered prompt + schema themselves, so a reworded prompt re-extracts
            # without a manual EXTRACT_SCHEMA_VERSION bump.
            prompt_sha=EXTRACTOR_PROMPT_SHA,
            schema_sha=EXTRACTOR_SCHEMA_SHA,
            sampling=regime,
            verifier=verifier_sig,
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
        # verifier changed since it was extracted, so fail loud rather than orphan the old
        # propositions (cascade re-extract is still future); absent → never extracted, so either
        # *replay* a prior identical-pipeline extraction (G1.7b cross-doc reuse, below) or run the
        # LLM. The hash is computed once here and reused at persist time so the stored key can never
        # drift from the decision it drove.
        verifier_sig = (
            {
                "model": self.verifier.llm.model,
                "schema_version": self.verifier.SCHEMA_VERSION,
                # G1.15: hash the verifier's actual prompt + schema too, so a reworded verifier
                # re-derives faithfulness instead of replaying a stale verdict.
                "prompt_sha": self.verifier.prompt_sha(),
                "schema_sha": self.verifier.schema_sha(),
            }
            if self.verifier is not None
            else None
        )
        pending: list[int] = []
        to_replay: list[tuple[int, ReusableExtraction]] = []
        hash_by_index: dict[int, str] = {}
        for i, s in enumerate(spans):
            chash = self._pipeline_hash(spans, i, raw_text, verifier_sig)
            hash_by_index[i] = chash
            stored = await self._extracted_hash(session, s.id)
            if stored == chash:
                continue  # already extracted with this exact pipeline: a true no-op.
            if stored is not None:
                raise StaleExtractionError(
                    f"span {s.id} was extracted under a different pipeline "
                    f"(stored {stored[:12]}…, now {chash[:12]}…); cascade re-extraction is not "
                    f"yet supported."
                )
            # Never extracted (stored is None). G1.7b: if an identical-pipeline extraction exists
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
            int, list[PropositionResult], list[tuple[uuid.UUID, uuid.UUID]], BaseException | None
        ]:
            try:
                results, twins = await self._infer_span(sem, spans, i, raw_text)
                return i, results, twins, None
            except Exception as exc:  # noqa: BLE001 — isolate; the span re-runs via idempotency
                logger.exception("extraction failed for span %s; isolating", spans[i].id)
                return i, [], [], exc

        inferred_raw = await asyncio.gather(*(infer(i) for i in pending))
        ok_raw: list[tuple[int, list[PropositionResult], list[tuple[uuid.UUID, uuid.UUID]]]] = []
        for i, results, twins, exc in inferred_raw:
            if exc is not None:
                failed_spans.append(FailedSpan(span_id=spans[i].id, phase="infer", error=str(exc)))
            else:
                ok_raw.append((i, results, twins))

        # Twins are carried alongside the per-span hash for persistence; the verify fan-out
        # below operates only on (index, results), so split them out here (G1.14).
        twins_by_index: dict[int, list[tuple[uuid.UUID, uuid.UUID]]] = {
            i: twins for i, _, twins in ok_raw
        }
        inferred = [(i, results) for i, results, _ in ok_raw]

        # Phase 2-replay (G1.7b): build replay results for the reusable spans — re-embed the cached
        # proposition text under the current substrate, no LLM and no verify (the reused
        # faithfulness was already computed on the source). DB-free, so it runs in the concurrent
        # phase; per-span isolated like inference (a failed replay records no Action → re-runs next
        # time, re-attempting reuse or falling through to a fresh extraction).
        async def replay(
            i: int, reusable: ReusableExtraction
        ) -> tuple[int, list[PropositionResult], ReusableExtraction, BaseException | None]:
            try:
                results = await self._build_replay_results(spans[i], reusable)
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
        # in the concurrent phase under the same budget. Absent verifier → verdicts stay
        # None and faithfulness/provisional remain null (the documented degraded mode).
        verified: list[tuple[int, list[PropositionResult], list[_VerifyOut | None] | None]]
        if self.verifier is None:
            verified = [(i, results, None) for i, results in inferred]
        else:
            verified = [
                (i, results, verdicts)
                for i, results, verdicts in await self._verify_all(sem, spans, raw_text, inferred)
            ]

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
                    )
                )
            except Exception as exc:  # noqa: BLE001 — isolate; roll back this span and continue
                await session.rollback()
                logger.exception("persist failed for span %s; isolating", spans[i].id)
                failed_spans.append(
                    FailedSpan(span_id=spans[i].id, phase="persist", error=str(exc))
                )

        # Phase 3-replay (G1.7b): persist the replayed spans, same per-span isolation. Each records
        # an extract Action with a reused_from pointer and no verify Action (the reused faithfulness
        # is already verified at the source) — but the same content_hash, so a re-run no-ops.
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
                    )
                )
            except Exception as exc:  # noqa: BLE001 — isolate; roll back this span and continue
                await session.rollback()
                logger.exception("replay persist failed for span %s; isolating", spans[i].id)
                failed_spans.append(
                    FailedSpan(span_id=spans[i].id, phase="persist", error=str(exc))
                )
        return PropositionizeReport(action_ids=action_ids, failed_spans=failed_spans)
