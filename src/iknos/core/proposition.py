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
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.consistency import (
    DEFAULT_AGREEMENT_THRESHOLD,
    Candidate,
    agreement_of,
    canonical_of,
    cluster_candidates,
)
from iknos.core.embeddings import EmbeddingSubstrate
from iknos.core.llm import LLMClient
from iknos.core.prompts import vocab
from iknos.db.orm import PropositionEmbedding
from iknos.provenance.action_log import record_action
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
    combine_faithfulness,
    faithfulness_from_verdict,
    is_provisional,
    route_for,
)
from iknos.types.nodes import Span

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


@dataclass(frozen=True)
class PropositionResult:
    """One extracted proposition with its provenance, epistemic fields, and vector.

    ``routing`` is derived from ``epistemic_class`` (G1.2). ``faithfulness``/``provisional`` are
    set by the verify fan-out (G1.4/G1.5) — null when no verifier is configured. ``agreement`` is
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
    provisional: bool | None = None
    agreement: float | None = None


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
    ) -> None:
        self.llm = llm
        self.substrate = substrate
        self.context_window = context_window
        self.concurrency = concurrency
        self.sampling = sampling or {"temperature": 0.0}
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
    ) -> list[PropositionResult]:
        """Extract one span's propositions, scoring each by cross-sample agreement (G1.3).

        Samples the extractor ``n_samples`` times, embeds every candidate, then clusters
        semantically-equivalent extractions; each cluster becomes one proposition carrying its
        medoid's text+operators and the fraction of samples that produced it (``agreement``). No
        DB access — concurrent-phase safe. The semaphore wraps each *individual* sample call (not
        the whole span), so the N samples of one span share the global budget with every other
        in-flight call and never deadlock by holding an outer permit while awaiting inner ones —
        the same permit discipline as ``_verify_all``.

        n_samples=1 short-circuits the clustering (1:1 candidate→proposition, ``agreement`` null),
        so single-pass behavior is byte-identical to pre-G1.3.
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
            return []

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

        # Single-pass: no clustering — each extraction is its own proposition (agreement null),
        # exactly as before G1.3. Multi-sample: cluster equivalent extractions and score agreement.
        if self.n_samples == 1:
            clusters = [[c] for c in candidates]
        else:
            clusters = cluster_candidates(candidates, threshold=self.agreement_threshold)

        results: list[PropositionResult] = []
        for cluster in clusters:
            canonical = canonical_of(cluster)
            agreement = (
                agreement_of(cluster, n_samples=self.n_samples) if self.n_samples > 1 else None
            )
            results.append(
                PropositionResult(
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
                    # Derived now (G1.2); faithfulness/provisional set by the verify pass (G1.4).
                    routing=route_for(canonical.epistemic_class),
                    agreement=agreement,
                )
            )
        return results

    async def _verify_all(
        self,
        sem: asyncio.Semaphore,
        spans: list[Span],
        raw_text: str,
        inferred: list[tuple[int, list[PropositionResult]]],
    ) -> list[tuple[int, list[PropositionResult], list["_VerifyOut"]]]:
        """Verify each inferred proposition against its source span (G1.4) and attach the
        derived faithfulness/provisional (G1.5).

        A separate concurrent fan-out, run after extraction completes and bounded by the
        *same* semaphore — each verify call acquires its own permit, so it never nests
        inside an extract permit (which would serialize throughput). Returns the results
        re-scored, paired with the raw verdicts for the verify Action.
        """
        verifier = self.verifier
        assert verifier is not None  # only called when a verifier is configured

        async def verify_one(
            source: str, r: PropositionResult
        ) -> tuple[PropositionResult, "_VerifyOut"]:
            async with sem:
                verdict = await verifier.verify_proposition(source, r)
            verify_component = faithfulness_from_verdict(
                verdict.entailment, verdict.polarity_preserved, verdict.modality_preserved
            )
            # Fold in the multi-sample agreement signal (G1.3). None ⇒ single-pass (N=1) ⇒
            # factor 1.0 ⇒ faithfulness == the verify component (unchanged from G1.4/G1.5).
            agreement = r.agreement if r.agreement is not None else 1.0
            faith = combine_faithfulness(verify_component, agreement)
            # PropositionResult is frozen — rebuild with the scored fields, never mutate.
            scored = replace(r, faithfulness=faith, provisional=is_provisional(faith))
            return scored, verdict

        async def verify_group(
            i: int, results: list[PropositionResult]
        ) -> tuple[int, list[PropositionResult], list["_VerifyOut"]]:
            source = span_text(raw_text, spans[i])
            pairs = await asyncio.gather(*(verify_one(source, r) for r in results))
            return i, [scored for scored, _ in pairs], [verdict for _, verdict in pairs]

        return list(await asyncio.gather(*(verify_group(i, results) for i, results in inferred)))

    async def _already_done(self, session: AsyncSession, span_id: uuid.UUID) -> bool:
        """True if this span was already propositionized (Action-based, covers empty spans)."""
        row = await session.execute(
            text(
                "SELECT 1 FROM actions WHERE actor = 'propositionizer' "
                "AND inputs->>'target_span' = :sid LIMIT 1"
            ),
            {"sid": str(span_id)},
        )
        return row.first() is not None

    async def _persist(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        span_id: uuid.UUID,
        context_span_ids: list[str],
        results: list[PropositionResult],
        verdicts: "list[_VerifyOut] | None" = None,
    ) -> uuid.UUID:
        """Persist one span's propositions + edges + indexes + Action in a single transaction.

        When verify verdicts are supplied (G1.4), a second Action (actor ``verifier``) records
        them in the *same* transaction — so a committed proposition's faithfulness always has
        an auditable verdict behind it. Returns the extract Action id.
        """
        from iknos.db.age import cypher_map, execute_cypher

        prop_ids: list[str] = []
        edge_ids: list[str] = []
        for r in results:
            # Epistemic fields (§3.1) as vertex properties: StrEnums serialize to
            # plain strings, None -> null (faithfulness/provisional are placeholders
            # owned by G1.4/G1.5/G1.6). routing is the derived fact/judgement tag (G1.2).
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
                "provisional": r.provisional,
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
                    proposition_id=r.id, document_id=document_id, embedding=r.embedding
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

        action_id = await record_action(
            session,
            actor="propositionizer",
            action_type="extract",
            inputs={"target_span": str(span_id), "context_spans": context_span_ids},
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
            await record_action(
                session,
                actor="verifier",
                action_type="verify",
                inputs={"target_span": str(span_id), "propositions": prop_ids},
                outputs={
                    "verdicts": [
                        {
                            "proposition": str(r.id),
                            "entailment": v.entailment,
                            "polarity_preserved": v.polarity_preserved,
                            "modality_preserved": v.modality_preserved,
                            "attribution_preserved": v.attribution_preserved,
                            "faithfulness": r.faithfulness,
                            "provisional": r.provisional,
                        }
                        for r, v in zip(results, verdicts, strict=True)
                    ]
                },
                model=self.verifier.llm.model,
                sampling=self.verifier.sampling,
            )

        await session.commit()
        return action_id

    async def propositionize_document(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        spans: list[Span],
        raw_text: str,
    ) -> list[uuid.UUID]:
        """Run the full pipeline for one document. Returns the Action ids produced."""
        # Phase 1: idempotency filter (serial reads on the shared session).
        pending = [i for i, s in enumerate(spans) if not await self._already_done(session, s.id)]

        # Phase 2: concurrent inference, bounded by a semaphore, with no DB access. The permit is
        # acquired *inside* _infer_span around each individual sample call (G1.3 fans out N per
        # span), so this coroutine must not hold one itself — that would deadlock at low
        # concurrency. Same permit discipline as the verify fan-out below.
        sem = asyncio.Semaphore(self.concurrency)

        async def infer(i: int) -> tuple[int, list[PropositionResult]]:
            return i, await self._infer_span(sem, spans, i, raw_text)

        inferred = await asyncio.gather(*(infer(i) for i in pending))

        # Phase 2b: independent verification (G1.4) — another DB-free LLM call, so it runs
        # in the concurrent phase under the same budget. Absent verifier → verdicts stay
        # None and faithfulness/provisional remain null (the documented degraded mode).
        verified: list[tuple[int, list[PropositionResult], list[_VerifyOut] | None]]
        if self.verifier is None:
            verified = [(i, results, None) for i, results in inferred]
        else:
            verified = [
                (i, results, verdicts)
                for i, results, verdicts in await self._verify_all(sem, spans, raw_text, inferred)
            ]

        # Phase 3: serial persistence — one short transaction per span.
        action_ids: list[uuid.UUID] = []
        for i, results, verdicts in verified:
            context_spans, _ = build_context(spans, i, raw_text, self.context_window)
            context_ids = [str(s.id) for s in context_spans]
            action_ids.append(
                await self._persist(
                    session, document_id, spans[i].id, context_ids, results, verdicts
                )
            )
        return action_ids
