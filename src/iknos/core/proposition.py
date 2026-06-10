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
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.embeddings import EmbeddingSubstrate
from iknos.core.llm import LLMClient
from iknos.db.orm import PropositionEmbedding
from iknos.provenance.action_log import record_action
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
    route_for,
)
from iknos.types.nodes import Span

# Note: iknos.db.age is imported lazily inside _persist so that importing this
# module does not pull in the config singleton (DATABASE_URL) — unit tests of the
# inference path stay DB-free.


def _vocab(enum: type[StrEnum]) -> str:
    """The legal value strings for an epistemic enum, for the prompt.

    Generated from the enum (not hand-typed) so the prompt's vocabulary can never
    drift from the guided-decode schema — a drift guided decoding would otherwise
    hide (the model is constrained to the schema's enum, so a stale prompt just
    biases classification silently).
    """
    return " / ".join(e.value for e in enum)


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
    f"- polarity ({_vocab(Polarity)}): whether the content is asserted or denied.\n"
    f"- modality ({_vocab(Modality)}): the claim's certainty.\n"
    f"- attribution ({_vocab(Attribution)}): asserted by the document itself, conveyed "
    "as reported speech, or a named source's claim.\n"
    "- scope: brief quantifier-scope notes, or empty string if none.\n"
    f"- epistemic_class ({_vocab(EpistemicClass)}): an objective observation/measurement, "
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

    ``faithfulness``/``provisional`` stay ``None`` in this increment — calibration
    is G1.4/G1.5 and the provisional gate is G1.6 (§3.1: confidence is not
    self-reported). ``routing`` is derived from ``epistemic_class`` (G1.2).
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
    ) -> None:
        self.llm = llm
        self.substrate = substrate
        self.context_window = context_window
        self.concurrency = concurrency
        self.sampling = sampling or {"temperature": 0.0}

    async def _infer_span(
        self, spans: list[Span], index: int, raw_text: str
    ) -> list[PropositionResult]:
        """LLM + embedding for one span. No DB access (safe to run concurrently)."""
        target = spans[index]
        _, context_text = build_context(spans, index, raw_text, self.context_window)
        messages = build_messages(context_text, span_text(raw_text, target))

        raw = await self.llm.guided_complete(messages, EXTRACTION_SCHEMA, self.sampling)
        extraction = PropositionExtraction.model_validate(raw)
        props = extraction.propositions
        if not props:
            return []

        # Sync torch forward pass — run off the event loop so concurrent LLM calls flow.
        vectors = await asyncio.to_thread(self.substrate.embed_passages, [p.text for p in props])
        return [
            PropositionResult(
                id=uuid.uuid4(),
                text=p.text,
                span_id=target.id,
                document_id=target.document_id,
                embedding=v,
                polarity=p.polarity,
                modality=p.modality,
                attribution=p.attribution,
                scope=p.scope,
                epistemic_class=p.epistemic_class,
                # Derived now (G1.2); faithfulness/provisional default None (G1.4/G1.5/G1.6).
                routing=route_for(p.epistemic_class),
            )
            for p, v in zip(props, vectors, strict=True)
        ]

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
    ) -> uuid.UUID:
        """Persist one span's propositions + edges + indexes + Action in a single transaction."""
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

        action_id = await record_action(
            session,
            actor="propositionizer",
            action_type="extract",
            inputs={"target_span": str(span_id), "context_spans": context_span_ids},
            outputs={"propositions": prop_ids, "edges": edge_ids},
            model=self.llm.model,
            sampling=self.sampling,
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

        # Phase 2: concurrent inference, bounded by a semaphore, with no DB access.
        sem = asyncio.Semaphore(self.concurrency)

        async def infer(i: int) -> tuple[int, list[PropositionResult]]:
            async with sem:
                return i, await self._infer_span(spans, i, raw_text)

        inferred = await asyncio.gather(*(infer(i) for i in pending))

        # Phase 3: serial persistence — one short transaction per span.
        action_ids: list[uuid.UUID] = []
        for i, results in inferred:
            context_spans, _ = build_context(spans, i, raw_text, self.context_window)
            context_ids = [str(s.id) for s in context_spans]
            action_ids.append(
                await self._persist(session, document_id, spans[i].id, context_ids, results)
            )
        return action_ids
