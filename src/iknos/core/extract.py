"""The ``extract`` operator core (Phase 2, G2.2; architecture.md §5, §6, §10).

Turns a **Proposition** (the Phase-1 value-layer unit) into a reasoning-graph
**Fact** carrying its **Actor**/**Object** entities (§5): the actors and objects are
*nodes*, not properties (§5/§10). For each proposition it writes, in one transaction:

- a ``:Fact`` vertex, boxed and tiered (tier resolved from the case box, §9), with the
  **two annotations initialized** (§12) and bitemporal fields stamped;
- one ``:Actor``/``:Object`` vertex per identified entity — **fresh nodes, no dedup**
  (entity resolution into ``SAME_AS`` components is G2.3; this slice never MERGEs an
  entity against an existing one);
- ``INVOLVES`` edges Fact→entity carrying the grammatical ``role`` (§10);
- ``EVIDENCED_BY`` edges Fact→Proposition and Fact→Span(s) — the provenance path that
  makes the Fact auditable to source (§10.2);
- an ``Action`` record (actor ``extractor``) naming inputs/outputs/model/sampling so the
  run is replayable and the Fact answers "where did you come from" (§10.1).

Pure/DB split (the ``core/proposition.py`` discipline): the schema, prompt assembly,
annotation seed, and the Fact write contract (``fact_to_props``) are DB- and LLM-free
so they unit-test without a graph; ``iknos.db.age`` is imported lazily inside ``_persist``
so importing this module never pulls in the ``DATABASE_URL`` config singleton.

Concurrency mirrors the proposition layer: a proposition's entity inference is a slow,
DB-free LLM call, so the run is three-phase — (1) serial idempotency filter against the
``Action`` log, (2) concurrent inference holding no DB session, (3) serial per-fact
persist, each committing in its own short transaction.

Scope deliberately left to later slices (documented seams):

- **Entity dedup / resolution** (scored ``SAME_AS`` components, anchor canonicalization)
  → G2.3. Here every mention is a fresh node.
- **Reference binding** (``Mention`` → ``REFERS_TO``) → G2.4.
- **Source credibility & sensitivity seeding** onto the Fact (§9.1) → G2.6. The Fact's
  ``sensitivity`` is left at the lattice origin (public) and its confidence is seeded only
  from the proposition's *faithfulness* (the one calibrated [0,1] available at extraction),
  not from box reliability.
- **The §5 observation/judgement split** ("a source's judgements are re-derived, not
  ingested as facts"). This slice materializes a Fact for every proposition; the
  ``epistemic_class``/``routing`` distinction is preserved via the ``EVIDENCED_BY``
  Proposition (reachable, not duplicated on the Fact), and treating judgement-claims as
  defeasible/credibility-weighted is the reasoning layer's job (Phase 3/4 + G2.6).
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import resolve_tier
from iknos.core.llm import LLMClient
from iknos.core.prompts import vocab
from iknos.provenance.action_log import record_action
from iknos.types.annotations import Annotations
from iknos.types.nodes import Box, Fact, Proposition, Tier
from iknos.types.temporal import BitemporalFields

# Note: iknos.db.age is imported lazily inside _persist (see module docstring), so
# importing this module stays DB-free for the unit tests of the pure/inference paths.


class NodeKind(StrEnum):
    """Which entity label a mention becomes (§5/§10) — the *kind* of node.

    Orthogonal to :class:`Role` (a grammatical relation): a person ``Actor`` may be the
    grammatical subject or object of a Fact, and a component ``Object`` likewise. A
    ``StrEnum`` so it serializes to a plain string for guided decoding / the prompt.
    """

    ACTOR = "actor"
    OBJECT = "object"


class Role(StrEnum):
    """The grammatical/semantic role an entity plays in a Fact — the ``INVOLVES.role``
    property (§10: "subject, object, instrument, …").

    ``SUBJECT`` is load-bearing beyond grammar: a Fact's abstraction level is later
    *derived* from its **subject-role** ``INVOLVES`` entity's position in the part-whole
    order (§14, G2.5). ``OTHER`` is the open-vocabulary catch-all so guided decoding never
    has to force an ill-fitting role.
    """

    SUBJECT = "subject"
    OBJECT = "object"
    INSTRUMENT = "instrument"
    LOCATION = "location"
    OTHER = "other"


# NodeKind -> AGE vertex label. The two entity labels exist in the initial migration
# (0001); this is the single mapping from the model enum to the graph label.
_AGE_LABEL: dict[NodeKind, str] = {
    NodeKind.ACTOR: "Actor",
    NodeKind.OBJECT: "Object",
}


class _EntityOut(BaseModel):
    """One entity as emitted by the extractor (drives guided decoding).

    Defaults keep a bare ``{"label": ...}`` response valid (mirrors
    ``proposition._PropositionOut``), so a minimal model response still validates. ``type``
    is the free-form domain entity type here — anchoring it to the active pack's taxonomy
    is G2.3, so this slice records whatever the model proposes (or empty).
    """

    label: str
    type: str = ""
    kind: NodeKind = NodeKind.OBJECT
    role: Role = Role.OTHER


class FactEntities(BaseModel):
    """Structured output contract for one proposition's entities; drives guided decoding."""

    entities: list[_EntityOut]


ENTITY_SCHEMA = FactEntities.model_json_schema()

# Bump on any change to the extractor output shape — the SYSTEM_PROMPT wording, the
# ENTITY_SCHEMA fields, or the NodeKind/Role enum sets interpolated into the prompt. Stored
# on each extract Action's inputs so a fact's producing pipeline is identifiable; mirrors
# proposition.EXTRACT_SCHEMA_VERSION. Cascade re-extraction under a changed pipeline is a
# later concern — this slice's idempotency only skips an already-extracted proposition.
EXTRACT_SCHEMA_VERSION = 1


SYSTEM_PROMPT = (
    "You identify the entities a single factual statement is about, so they can become "
    "typed nodes in a reasoning graph.\n"
    "Rules:\n"
    "- Extract the ACTORS (agents that act or hold a stance: people, organizations, "
    "roles, systems) and the OBJECTS (things acted upon or referred to: components, "
    "artifacts, materials, measurements, concepts) that the statement involves.\n"
    "- Entities are NODES, not adjectives: extract the bearing, the operator, the report "
    "— not bare descriptive words.\n"
    "- Use the statement's own surface form for `label` (you may normalize trivially, e.g. "
    "drop a leading article); do not invent entities the statement does not mention.\n"
    "- If the statement mentions no concrete entity, return an empty list.\n"
    "Per-entity fields:\n"
    f"- kind ({vocab(NodeKind)}): an acting agent vs a thing acted upon/referred to.\n"
    f"- role ({vocab(Role)}): the entity's grammatical/semantic role in THIS statement "
    "(the subject is the entity the statement is primarily about).\n"
    "- type: a short domain type for the entity (e.g. 'bearing', 'person', "
    "'organization'), or empty string if unclear.\n"
    'Example: "The operator restarted the pump." -> {"entities": ['
    '{"label": "operator", "type": "person", "kind": "actor", "role": "subject"}, '
    '{"label": "pump", "type": "equipment", "kind": "object", "role": "object"}]}.\n'
    'Return JSON of the form {"entities": [{"label": "...", "type": "...", '
    '"kind": "...", "role": "..."}]}.'
)


def build_messages(statement: str) -> list[dict[str, str]]:
    """Assemble the chat messages for one proposition's entity extraction."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"STATEMENT:\n{statement}"},
    ]


def seed_confidence(faithfulness: float | None) -> float:
    """The Layer-B confidence a base Fact is *initialized* with (§12) — a seed, not the
    computed value.

    The real Layer-B confidence is the least-fixpoint valuation over the well-founded
    support, computed in Phase 3 (``core/confidence.py``); extraction only fills the slot
    so "both annotations from day one" holds (§12). For a base fact extracted from a single
    proposition the only calibrated [0,1] available is the proposition's **faithfulness**
    (how faithfully it represents its span, §3.1), so that is the seed. When no verifier
    ran (faithfulness ``None``) the seed is ``1.0`` — the Viterbi semiring's multiplicative
    identity (``one``): "no calibrated discount yet", never a self-reported confidence.
    """
    return 1.0 if faithfulness is None else faithfulness


def base_annotations(faithfulness: float | None) -> Annotations:
    """The two annotations a base Fact starts with (§12).

    ``support_count = 1``: a base fact is grounded by exactly one piece of evidence (its
    ``EVIDENCED_BY`` proposition) — the Layer-A grounding anchor; when that support is
    retracted the count drops to 0 and the fact becomes unsupported. ``confidence`` is the
    :func:`seed_confidence` seed. The pair is never collapsed into one number (§12).
    """
    return Annotations(support_count=1, confidence=seed_confidence(faithfulness))


def fact_to_props(fact: Fact) -> dict[str, Any]:
    """Flatten a :class:`Fact` to AGE vertex properties — the canonical Fact write contract.

    The single place Fact serialization lives (cf. ``boxes/serde.box_to_props`` for boxes),
    so every Fact writer shares one mapping. Annotations flatten to ``support_count`` /
    ``confidence`` (§12); bitemporal fields to ISO-8601 (null where open); sensitivity via
    its canonical flat names (§9.1). The soft-override slot (§10.3) is null on a
    machine-produced Fact, so it is omitted here rather than written as null.
    """
    props: dict[str, Any] = {
        "id": str(fact.id),
        "box": str(fact.box),
        "tier": str(fact.tier),
        "statement": fact.statement,
        "support_count": fact.annotations.support_count,
        "confidence": fact.annotations.confidence,
        "event_time": (
            fact.temporal.event_time.isoformat() if fact.temporal.event_time is not None else None
        ),
        "ingested_at": fact.temporal.ingested_at.isoformat(),
        "valid_from": fact.temporal.valid_from.isoformat(),
        "valid_to": (
            fact.temporal.valid_to.isoformat() if fact.temporal.valid_to is not None else None
        ),
    }
    props.update(fact.sensitivity.flatten())
    return props


@dataclass(frozen=True)
class ExtractedEntity:
    """One entity resolved into a fresh node (no dedup): assigned id + its role/kind."""

    id: uuid.UUID
    label: str
    type: str
    kind: NodeKind
    role: Role


@dataclass(frozen=True)
class ExtractInput:
    """One unit of work for the operator: a Proposition plus the Span(s) it is evidenced by.

    ``span_ids`` are the proposition's ``EVIDENCED_BY`` Spans (provenance the Fact inherits);
    the Fact gets its own ``EVIDENCED_BY`` to the Proposition *and* to each Span so it is
    traceable to source text without walking through the Proposition (§10.2).
    """

    proposition: Proposition
    span_ids: list[uuid.UUID]


class Extractor:
    """The ``extract`` operator (§6): Proposition → Fact + Actor/Object nodes.

    DB-free to construct; the LLM identifies the entities and the graph writes happen in
    ``_persist``. Stateless across calls beyond its injected LLM + sampling regime.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        sampling: dict[str, object] | None = None,
        concurrency: int = 8,
    ) -> None:
        self.llm = llm
        self.sampling = sampling or {"temperature": 0.0}
        self.concurrency = concurrency

    async def _infer(self, sem: asyncio.Semaphore, statement: str) -> list[ExtractedEntity]:
        """Identify one statement's entities via guided decoding (LLM, DB-free).

        Each entity gets a **fresh** uuid — this slice does no dedup, so two mentions of the
        same real entity become two nodes (G2.3 resolves them into a ``SAME_AS`` component).
        The semaphore bounds the global LLM concurrency, acquired around the single call so
        it never nests inside another permit (the proposition-layer permit discipline).
        """
        messages = build_messages(statement)
        async with sem:
            raw = await self.llm.guided_complete(messages, ENTITY_SCHEMA, self.sampling)
        out = FactEntities.model_validate(raw)
        return [
            ExtractedEntity(
                id=uuid.uuid4(),
                label=e.label,
                type=e.type,
                kind=e.kind,
                role=e.role,
            )
            for e in out.entities
        ]

    async def _already_extracted(self, session: AsyncSession, proposition_id: uuid.UUID) -> bool:
        """Whether this proposition already produced a Fact (idempotency, §10.1-backed).

        Action-table backed (single source of truth), mirroring ``proposition._extracted_hash``.
        A proposition maps to exactly one Fact in this slice, so an existing ``extract`` Action
        by ``extractor`` for it means re-running is a true no-op — re-extraction under a changed
        entity pipeline (cascade) is a later concern.
        """
        row = await session.execute(
            text(
                "SELECT 1 FROM actions WHERE actor = 'extractor' AND action_type = 'extract' "
                "AND inputs->>'proposition' = :pid LIMIT 1"
            ),
            {"pid": str(proposition_id)},
        )
        return row.scalar_one_or_none() is not None

    async def _persist(
        self,
        session: AsyncSession,
        item: ExtractInput,
        box: Box,
        entities: list[ExtractedEntity],
        *,
        tier_override: Tier | None = None,
    ) -> uuid.UUID:
        """Persist one proposition's Fact + entities + edges + Action in one transaction.

        Returns the extract Action id. Commits — one short transaction per fact, like the
        proposition layer's ``_persist``.
        """
        from iknos.db.age import merge_edge, merge_vertex

        prop = item.proposition
        now = datetime.now(UTC)
        fact = Fact(
            id=uuid.uuid4(),
            box=box.id,
            tier=resolve_tier(box, tier_override),
            statement=prop.text,
            annotations=base_annotations(prop.faithfulness),
            temporal=BitemporalFields(ingested_at=now, valid_from=now),
            # sensitivity left at the lattice origin — source-sensitivity seeding is G2.6.
        )
        await merge_vertex(session, "Fact", fact_to_props(fact))

        # Entities as fresh Actor/Object vertices (no dedup, G2.3), with role-tagged INVOLVES.
        for e in entities:
            await merge_vertex(
                session,
                _AGE_LABEL[e.kind],
                {"id": str(e.id), "box": str(box.id), "label": e.label, "type": e.type},
            )
            await merge_edge(
                session,
                src_id=fact.id,
                dst_id=e.id,
                label="INVOLVES",
                props={"role": str(e.role), "box": str(box.id)},
            )

        # Provenance (§10.2): Fact -> its Proposition and -> each source Span.
        await merge_edge(
            session,
            src_id=fact.id,
            dst_id=prop.id,
            label="EVIDENCED_BY",
            props={"box": str(box.id)},
        )
        for sid in item.span_ids:
            await merge_edge(
                session,
                src_id=fact.id,
                dst_id=sid,
                label="EVIDENCED_BY",
                props={"box": str(box.id)},
            )

        action_id = await record_action(
            session,
            actor="extractor",
            action_type="extract",
            inputs={
                "proposition": str(prop.id),
                "spans": [str(s) for s in item.span_ids],
                "box": str(box.id),
                "schema_version": EXTRACT_SCHEMA_VERSION,
            },
            outputs={
                "fact": str(fact.id),
                "actors": [str(e.id) for e in entities if e.kind is NodeKind.ACTOR],
                "objects": [str(e.id) for e in entities if e.kind is NodeKind.OBJECT],
                "involves": [f"{fact.id}->{e.id}" for e in entities],
                "evidenced_by": (
                    [f"{fact.id}->{prop.id}"] + [f"{fact.id}->{s}" for s in item.span_ids]
                ),
            },
            model=self.llm.model,
            sampling=self.sampling,
        )
        await session.commit()
        return action_id

    async def extract_propositions(
        self,
        session: AsyncSession,
        items: list[ExtractInput],
        box: Box,
        *,
        tier_override: Tier | None = None,
    ) -> list[uuid.UUID]:
        """Extract Facts for a batch of propositions into ``box``. Returns the Action ids.

        Three-phase, like ``propositionize_document`` (the shared session is unsafe for
        concurrent use): (1) serial idempotency filter — drop propositions that already
        produced a Fact; (2) concurrent entity inference holding no DB session, bounded by a
        semaphore; (3) serial per-fact persist, each its own short transaction. Skipped
        (already-extracted) propositions contribute no Action id.
        """
        # Phase 1: idempotency filter (serial reads on the shared session).
        pending: list[ExtractInput] = []
        for item in items:
            if not await self._already_extracted(session, item.proposition.id):
                pending.append(item)

        # Phase 2: concurrent inference, DB-free, bounded by a single shared semaphore.
        sem = asyncio.Semaphore(self.concurrency)
        inferred = await asyncio.gather(
            *(self._infer(sem, item.proposition.text) for item in pending)
        )

        # Phase 3: serial persistence — one short transaction per fact.
        action_ids: list[uuid.UUID] = []
        for item, entities in zip(pending, inferred, strict=True):
            action_ids.append(
                await self._persist(session, item, box, entities, tier_override=tier_override)
            )
        return action_ids

    async def extract_proposition(
        self,
        session: AsyncSession,
        item: ExtractInput,
        box: Box,
        *,
        tier_override: Tier | None = None,
    ) -> uuid.UUID | None:
        """Single-proposition convenience (the §6 per-node operator shape).

        Returns the extract Action id, or ``None`` if the proposition was already extracted.
        """
        ids = await self.extract_propositions(session, [item], box, tier_override=tier_override)
        return ids[0] if ids else None
