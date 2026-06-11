"""The ``deduce`` / ``induce`` derivation operators (Phase 3, G3.8; architecture §6, §10.2, §12).

The §6 operators that turn supported reasoning nodes into new **conclusions**: ``deduce``
produces a :class:`~iknos.types.nodes.Conclusion` labelled ``DeductiveConclusion`` (a sound
step), ``induce`` one labelled ``InductiveConclusion`` and **marked provisional** (a
defeasible generalization). Premises may be Facts *or* prior conclusions (chaining), so the
operators compose into multi-step derivations.

**"LLM proposes, engine disposes" (the day-0 constraint).** This module is the **engine**.
It takes a :class:`DerivationProposal` — the candidate (premise nodes, claim text, kind,
step strength) an upstream proposer (an LLM, a domain rule, an expert) puts forward — and
*disposes* of it: it writes the conclusion's **structure** (the node + ``DERIVED_FROM``
group honouring the G3.4 grouping contract), then computes the conclusion's two §12
annotations from the **engine**, never the proposer:

* ``support_count`` — Layer A's grounding multiplicity over the well-founded support
  (``core/truth_maintenance.py``);
* ``confidence`` — Layer B's least-fixpoint valuation over that support
  (``core/confidence.py``), **not** any number the proposer supplied.

So no proposer output mutates maintained reasoning state directly: the claim text is
content, but membership and strength are recomputed. A conclusion whose premises are not
(yet) supported is still written — its structure is valid — but lands ``support_count = 0``
/ ``confidence = 0`` and revives if a premise later grounds (the Layer A semantics).

**One transaction, computed on the augmented graph (no write-then-patch).** The operator
loads the active subgraph (G3.4 adapter), augments it *in memory* with the proposed
derivation, runs Layer A + Layer B on the augmented graph to get the conclusion's
annotations, then writes the node **once** with those final annotations plus its
``DERIVED_FROM`` edges and an ``Action`` — all in one transaction. Same-session reads make
the structure visible to any follow-up within the txn; the commit is atomic.

**Provenance (§10.2).** The conclusion is traceable to source two ways: structurally via
``conclusion -[:DERIVED_FROM]-> premise -[:EVIDENCED_BY]-> Span`` (the graph path), and in
the audit log via the ``Action`` recording the premises and their source spans. No
``EVIDENCED_BY`` is written *from* a conclusion — that edge marks base facts, and adding it
would make the adapter misread a derived node as evidence-grounded.

Pure/DB split (the ``core/extract.py`` discipline): the write contracts
(:func:`conclusion_to_props`, :func:`derivation_edge_props`) and the annotation computation
(:func:`value_conclusion`) are DB-free and unit-testable; only :class:`Deriver`'s methods
touch AGE, via the lazily-imported ``iknos.db.age`` and the G3.4 adapter.

Scope deliberately left to later increments (documented seams):

- **The LLM/rule proposer** that *generates* :class:`DerivationProposal`s from the graph
  (which premises, what claim, what step strength) — hypothesis/derivation generation is its
  own concern (Phase 4-adjacent). This operator deliberately accepts a pre-formed proposal so
  the "engine disposes" boundary is explicit and the engine is testable without an LLM.
- **Conclusion dedup / disjunctive accrual** — each ``derive`` call mints a *fresh*
  conclusion node, so two independent derivations of the *same claim* are two nodes, not one
  node with ``support_count = 2``. Recognizing same-claim conclusions (so disjunctive support
  accrues to one node) is the conclusion analogue of entity resolution, tied to G3.7.
- **Annotation propagation to *existing* affected nodes** — a new derivation can only *add*
  support, so it never lowers an existing node's annotations; recomputing and rewriting every
  changed node's stored annotations on each derivation is the incremental persisted-write path
  (with G3.3). This operator writes the new conclusion's annotations; downstream reads recompute
  via the adapter regardless.
- **Per-antecedent edge strength** — one ``strength`` per derivation step (stored on each
  edge of the group), per the G3.4 contract; varying it per premise is a §7.1 refinement.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import resolve_tier
from iknos.core.confidence import DEFAULT_SEMIRING, Semiring, valuate
from iknos.core.derivation_adapter import ActiveSubgraph, DerivationGraphAdapter
from iknos.core.truth_maintenance import Derivation, DerivationGraph, IncrementalOracle
from iknos.provenance.action_log import record_action
from iknos.types.annotations import Annotations
from iknos.types.nodes import Box, Conclusion, Tier
from iknos.types.temporal import BitemporalFields

# Note: iknos.db.age is imported lazily inside the DB methods (the extract.py discipline),
# so importing this module stays DB-free for the unit tests of the pure paths.

# Bump on any change to the derivation write contract (the conclusion/edge property shape,
# the annotation-computation seam). Stored on each derive Action so a conclusion's producing
# pipeline is identifiable (mirrors extract.EXTRACT_SCHEMA_VERSION).
DERIVE_SCHEMA_VERSION = 1


class DerivationKind(StrEnum):
    """Deductive (sound) vs inductive (provisional generalization) derivation (§6)."""

    DEDUCTIVE = "deductive"
    INDUCTIVE = "inductive"


#: Derivation kind → AGE conclusion label. Both labels exist in the initial migration (0001).
_AGE_LABEL: dict[DerivationKind, str] = {
    DerivationKind.DEDUCTIVE: "DeductiveConclusion",
    DerivationKind.INDUCTIVE: "InductiveConclusion",
}


@dataclass(frozen=True)
class DerivationProposal:
    """The "LLM proposes" half: a candidate conclusion the engine will dispose of.

    ``statement`` is the proposed claim text (content); ``premise_ids`` are the existing
    reasoning nodes it is derived from (the conjunctive body); ``kind`` selects deductive vs
    inductive; ``strength`` is the inference *step*'s ``[0, 1]`` confidence (the §7.1
    ``DERIVED_FROM`` edge strength — the rule's reliability, **not** the conclusion's
    confidence, which Layer B computes). A deductive step is typically ``1.0`` (soundness:
    confidence flows from the premises); an inductive one is ``< 1.0`` (the generalization
    is uncertain).
    """

    statement: str
    premise_ids: tuple[uuid.UUID, ...]
    kind: DerivationKind
    strength: float


@dataclass(frozen=True)
class DerivationResult:
    """The outcome of one ``derive``: the conclusion id, its computed annotations, the
    ``DERIVED_FROM`` group id, and the ``Action`` id (so the write is auditable, §10.1)."""

    conclusion_id: uuid.UUID
    action_id: uuid.UUID
    derivation_group: uuid.UUID
    support_count: int
    confidence: float


def conclusion_to_props(conclusion: Conclusion) -> dict[str, Any]:
    """Flatten a :class:`Conclusion` to AGE vertex properties — the canonical write contract.

    Mirrors ``extract.fact_to_props`` (the single place each node's serialization lives) plus
    the ``provisional`` flag. Annotations flatten to ``support_count`` / ``confidence`` (§12,
    here the *computed* Layer A/B values); bitemporal fields to ISO-8601 (null where open);
    sensitivity via its canonical flat names (§9.1). The soft-override slot (§10.3) is null on
    a machine-produced conclusion, so it is omitted rather than written as null.
    """
    props: dict[str, Any] = {
        "id": str(conclusion.id),
        "box": str(conclusion.box),
        "tier": str(conclusion.tier),
        "statement": conclusion.statement,
        "provisional": conclusion.provisional,
        "support_count": conclusion.annotations.support_count,
        "confidence": conclusion.annotations.confidence,
        "event_time": (
            conclusion.temporal.event_time.isoformat()
            if conclusion.temporal.event_time is not None
            else None
        ),
        "ingested_at": conclusion.temporal.ingested_at.isoformat(),
        "valid_from": conclusion.temporal.valid_from.isoformat(),
        "valid_to": (
            conclusion.temporal.valid_to.isoformat()
            if conclusion.temporal.valid_to is not None
            else None
        ),
    }
    props.update(conclusion.sensitivity.flatten())
    return props


def derivation_edge_props(
    *, box: uuid.UUID, group: uuid.UUID, strength: float, now: datetime
) -> dict[str, Any]:
    """Flatten one ``DERIVED_FROM`` edge to AGE properties — the G3.4 grouping contract.

    Every edge of a single derivation shares the ``derivation`` **group id** (so the adapter
    regroups them into one conjunctive body) and the same step ``strength`` (§7.1). Bitemporal
    fields are stamped open (``valid_to``/``event_time`` null) so retraction stamps ``valid_to``
    and the adapter's current-state filter drops the edge.
    """
    return {
        "box": str(box),
        "derivation": str(group),
        "strength": strength,
        "event_time": None,
        "ingested_at": now.isoformat(),
        "valid_from": now.isoformat(),
        "valid_to": None,
    }


def value_conclusion(
    subgraph: ActiveSubgraph,
    derivation: Derivation,
    strength: float,
    *,
    semiring: Semiring = DEFAULT_SEMIRING,
) -> tuple[int, float]:
    """Compute a proposed conclusion's two §12 annotations from the engine (DB-free, pure).

    Augments the loaded ``subgraph`` *in memory* with ``derivation`` (and its ``strength``),
    runs Layer A (an :class:`IncrementalOracle` applied once = a recompute) for membership and
    grounding multiplicity, then Layer B (:func:`~iknos.core.confidence.valuate`) for strength
    over exactly the certified set. Returns ``(support_count, confidence)``:

    * if the conclusion is **well-founded** in the augmented graph → its Layer A
      ``support_count`` and Layer B confidence;
    * otherwise (a premise is unsupported) → ``(0, 0.0)`` — the structure is valid but
      ungrounded, and it revives if a premise later grounds (foundedness gates both, §12).
    """
    graph = DerivationGraph(
        base_facts=subgraph.graph.base_facts,
        derivations=(*subgraph.graph.derivations, derivation),
    )
    strength_map = dict(subgraph.strength)
    strength_map[derivation] = strength

    oracle = IncrementalOracle()
    supported = oracle.apply(graph)
    if derivation.conclusion not in supported:
        return 0, 0.0
    confidence = valuate(
        graph,
        supported,
        base_confidence=subgraph.base_confidence,
        strength=strength_map,
        semiring=semiring,
    )
    return oracle.support_count(derivation.conclusion), confidence[derivation.conclusion]


class Deriver:
    """The ``deduce``/``induce`` engine operator (§6): premises → certified, valued conclusion.

    DB-free to construct (it carries a :class:`DerivationGraphAdapter` and a semiring choice);
    the graph reads/writes happen in :meth:`derive`. Stateless across calls — each derivation
    is its own short transaction, like ``extract``'s per-fact persist.
    """

    def __init__(
        self,
        *,
        adapter: DerivationGraphAdapter | None = None,
        semiring: Semiring = DEFAULT_SEMIRING,
    ) -> None:
        self.adapter = adapter or DerivationGraphAdapter()
        self.semiring = semiring

    async def _premise_spans(
        self, session: AsyncSession, premise_ids: tuple[uuid.UUID, ...]
    ) -> dict[str, list[str]]:
        """The source spans each premise Fact is ``EVIDENCED_BY`` — the §10.2 provenance trace.

        Recorded on the derive ``Action`` so a conclusion is auditable to source text. A
        conclusion premise (no ``EVIDENCED_BY``) contributes none here; its own provenance is
        the recursive ``DERIVED_FROM`` chain.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        spans: dict[str, list[str]] = {}
        for pid in premise_ids:
            rows = await execute_cypher(
                session,
                f"MATCH (f {{id: '{pid}'}})-[:EVIDENCED_BY]->(s:Span) RETURN s.id",
                returns="sid agtype",
            )
            if rows:
                spans[str(pid)] = [unquote_agtype(sid) for (sid,) in rows]
        return spans

    async def derive(
        self,
        session: AsyncSession,
        proposal: DerivationProposal,
        box: Box,
        *,
        tier_override: Tier | None = None,
    ) -> DerivationResult:
        """Dispose of one proposal: write the conclusion + ``DERIVED_FROM`` group + ``Action``,
        with the two §12 annotations computed by Layer A/B. One transaction.

        Loads the current active subgraph, values the proposed derivation on the augmented
        graph (:func:`value_conclusion`), then writes the conclusion **once** with those final
        annotations, the ``DERIVED_FROM`` edges (one group id, the step strength), and the
        provenance-bearing ``Action`` — committing atomically.
        """
        from iknos.db.age import merge_edge, merge_vertex

        subgraph = await self.adapter.load_active(session)

        cid = uuid.uuid4()
        derivation = Derivation(
            conclusion=str(cid),
            body=frozenset(str(p) for p in proposal.premise_ids),
        )
        support_count, confidence = value_conclusion(
            subgraph, derivation, proposal.strength, semiring=self.semiring
        )

        now = datetime.now(UTC)
        conclusion = Conclusion(
            id=cid,
            box=box.id,
            tier=resolve_tier(box, tier_override),
            statement=proposal.statement,
            provisional=proposal.kind is DerivationKind.INDUCTIVE,
            annotations=Annotations(support_count=support_count, confidence=confidence),
            temporal=BitemporalFields(ingested_at=now, valid_from=now),
            # sensitivity left at the lattice origin — lub propagation over premises is §9.1.
        )
        await merge_vertex(session, _AGE_LABEL[proposal.kind], conclusion_to_props(conclusion))

        group = uuid.uuid4()
        edge_props = derivation_edge_props(
            box=box.id, group=group, strength=proposal.strength, now=now
        )
        for pid in proposal.premise_ids:
            await merge_edge(
                session, src_id=cid, dst_id=pid, label="DERIVED_FROM", props=edge_props
            )

        spans = await self._premise_spans(session, proposal.premise_ids)
        action_id = await record_action(
            session,
            actor="deriver",
            action_type=str(proposal.kind),
            inputs={
                "premises": [str(p) for p in proposal.premise_ids],
                "spans": spans,
                "box": str(box.id),
                "strength": proposal.strength,
                "kind": str(proposal.kind),
                "schema_version": DERIVE_SCHEMA_VERSION,
            },
            outputs={
                "conclusion": str(cid),
                "derivation_group": str(group),
                "derived_from": [f"{cid}->{p}" for p in proposal.premise_ids],
                "support_count": support_count,
                "confidence": confidence,
            },
        )
        await session.commit()
        return DerivationResult(
            conclusion_id=cid,
            action_id=action_id,
            derivation_group=group,
            support_count=support_count,
            confidence=confidence,
        )

    async def deduce(
        self,
        session: AsyncSession,
        statement: str,
        premise_ids: tuple[uuid.UUID, ...],
        box: Box,
        *,
        strength: float = 1.0,
        tier_override: Tier | None = None,
    ) -> DerivationResult:
        """``deduce`` (§6): a sound :class:`DeductiveConclusion` from its premises.

        Default ``strength = 1.0`` — a sound step adds no discount of its own; the
        conclusion's confidence flows from the premises (the weakest link, under the Gödel
        default).
        """
        proposal = DerivationProposal(
            statement=statement,
            premise_ids=premise_ids,
            kind=DerivationKind.DEDUCTIVE,
            strength=strength,
        )
        return await self.derive(session, proposal, box, tier_override=tier_override)

    async def induce(
        self,
        session: AsyncSession,
        statement: str,
        premise_ids: tuple[uuid.UUID, ...],
        box: Box,
        *,
        strength: float,
        tier_override: Tier | None = None,
    ) -> DerivationResult:
        """``induce`` (§6): a **provisional** :class:`InductiveConclusion` (a generalization).

        ``strength`` is required (no sound default): an inductive step's reliability is a
        genuine epistemic input, the discount the §7.1 edge confidence records.
        """
        proposal = DerivationProposal(
            statement=statement,
            premise_ids=premise_ids,
            kind=DerivationKind.INDUCTIVE,
            strength=strength,
        )
        return await self.derive(session, proposal, box, tier_override=tier_override)
