"""Phase 4 candidate generation (G4.2; architecture §5.1).

Edge adjudication (the §8 sign+strength LLM judgment, G4.3) is expensive, so it must run only
on pairs worth assessing — all-pairs is ``O(n²)`` LLM calls, intractable at scale, and most
pairs are unrelated. **Candidate generation (cheap, high-recall, approximate) and edge
adjudication (expensive, high-precision, LLM) are two separate stages** (§5.1); this module is
the first. It is the standard *blocking / candidate-generation* funnel from entity resolution
and link prediction: spend compute in inverse proportion to how many pairs survive each cheap
stage, and let the LLM stage do precision.

A *candidate* is an ordered ``(evidence → hypothesis)`` pair — the schema direction a
``SUPPORTS``/``REFUTES`` edge would run (§5, §10), so generation already knows which node is the
bearer and which is the target. ``sign`` is **not** decided here (that is the §8 "sign before
magnitude" judgment, G4.3); generation only proposes *which pairs to look at*.

**The funnel, cheap → expensive (§5.1).** Stage 4 (LLM adjudication) is G4.3; this increment
ships the recall-first funnel core and **stage 1, the structural-entity prior** — the refuter-safe
recall floor — with the other cheap generators as documented seams:

1. **Structural priors** — near-free. Two reasoning nodes sharing an ``Actor``/``Object`` (via an
   ``INVOLVES`` edge), restricted to the active box scope, are candidates. *(Shipped:
   :func:`structural_entity_candidates`. The sparse/keyword co-occurrence prior is a further
   :class:`CandidateSource` that unions in at the same seam.)*
2. **Embedding nearest-neighbour** — each node's pgvector k-NN are relatedness candidates;
   sublinear, the workhorse. *(Deferred slice-2 seam: needs the cross-store pgvector read +
   span/proposition → reasoning-node tracing.)*
3. **Coarse-to-fine** — reuse the §2 multi-level chunk hierarchy as a pruning tree: match coarse,
   descend to proposition pairing only within survivors. *(Deferred slice-2 seam: needs the
   ``partOf`` level derivation.)*

**Tune for recall early, precision late (§5.1).** A missed candidate is an edge never considered
— a silent false negative, the dangerous kind; a spurious candidate is just cheaply rejected at
adjudication. So the cheap stages favour recall: this layer does **not** score or rank candidates
(precision is the LLM stage's job), and the funnel **unions** generators rather than intersecting
them (the :data:`DEFAULT_STRATEGY` decision below).

**The dissimilar-refuter problem (§5.1) — why the structural prior ships first and why the funnel
unions.** A *refuting* fact can be semantically dissimilar to the hypothesis it attacks, so
embedding-NN under-generates refutation candidates and would bias the system toward finding
support and missing contradiction — a serious flaw given how central refutation is. The structural
prior pulls candidates by a hypothesis's *constituent entities*, not its embedding, so it catches
the dissimilar refuter the embedding stage misses; **intersecting** the two stages would drop it
again. Hence the recall-first union default. (``find-contradiction`` as a first-class refuter
*generator* is G4.5; this is the structural half of the mitigation.)

Pure/DB split mirrors ``core/qbaf_adapter.py``: the value types and the funnel/stage logic are
DB-free (unit-testable with hand-built rows); :class:`CandidateGenerationAdapter` does the AGE
reads in its ``async`` methods behind a lazy ``iknos.db.age`` import, reusing the shared
``load_active_box_ids`` / ``load_reasoning_nodes`` / ``load_hypothesis_ids`` reads so the
active-subgraph definition cannot diverge from the propagation/adjudication loads.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from iknos.core.derivation_adapter import (
    load_active_box_ids,
    load_hypothesis_ids,
    load_reasoning_nodes,
)
from iknos.core.truth_maintenance import NodeId


class CandidateSource(StrEnum):
    """Which funnel generator proposed a pair (§5.1) — provenance, not a score.

    A pair may be proposed by several generators; the funnel unions their sources onto one
    :class:`Candidate` (a pair found by *both* the structural and embedding stages is one
    candidate carrying both sources, not two). Adding a member is additive — a new cheap
    generator unions in at the funnel seam without changing the contract.
    """

    STRUCTURAL_ENTITY = "structural-entity"  # shared INVOLVES Actor/Object (shipped — stage 1)
    # Deferred cheap generators (slice-2 seams; see module docstring):
    # STRUCTURAL_KEYWORD = "structural-keyword"  # sparse/keyword co-occurrence (stage 1)
    # EMBEDDING_KNN = "embedding-knn"            # pgvector nearest-neighbour (stage 2)


class FunnelStrategy(StrEnum):
    """How :func:`funnel` combines the cheap generators' outputs (§5.1).

    The decision parallels the Layer-B (Gödel over Viterbi), QBAF (DF-QuAD over Quadratic Energy)
    and fusion (averaging over cumulative) choices — *default to the operator that cannot lose a
    true candidate; retain the other at the seam* — decided here with the
    :func:`~tests.unit.test_candidates` dissimilar-refuter fixture:

    - **``UNION``** (the recall-first default) — a pair any generator proposes survives, sources
      merged. The dissimilar refuter (structurally related, embedding-dissimilar) is caught by
      the structural stage and **kept**; this is the §5.1 "recall early" discipline and the
      structural half of the dissimilar-refuter mitigation.
    - **``INTERSECT``** — keep only pairs every generator proposed. This **drops the dissimilar
      refuter** (the embedding stage never proposed it), re-introducing exactly the support-bias
      §5.1 forbids — so it is *never* the candidate-generation default. Retained at the seam as a
      precision pre-filter for a recall-saturated sub-domain only.
    """

    UNION = "union"
    INTERSECT = "intersect"


DEFAULT_STRATEGY = FunnelStrategy.UNION
"""Recall-first union (§5.1) — never lose a candidate a cheap stage found."""


@dataclass(frozen=True)
class Candidate:
    """One ``(evidence → hypothesis)`` pair worth adjudicating (§5.1, §5).

    ``evidence`` is the bearer (a ``Fact``/``Conclusion``), ``hypothesis`` the target it bears on
    — the schema direction a ``SUPPORTS``/``REFUTES`` edge would run (§5, §10). The pair is
    **unscored**: candidate generation tunes for recall, leaving precision (and ``sign``) to the
    §8 LLM judgment (G4.3). ``sources`` records which generators proposed it; ``shared_entities``
    is the structural prior's rationale (the ``Actor``/``Object`` ids linking the pair, empty for
    a source that supplies none) — provenance the §8 *relative* judgment can rank within.
    """

    evidence: NodeId
    hypothesis: NodeId
    sources: frozenset[CandidateSource]
    shared_entities: frozenset[NodeId] = field(default_factory=frozenset)

    @property
    def key(self) -> tuple[NodeId, NodeId]:
        """The pair identity the funnel dedups on — direction included (§5)."""
        return (self.evidence, self.hypothesis)


@dataclass(frozen=True)
class CandidatePool:
    """The deduped set of candidates surviving the funnel — the input to §8 adjudication (G4.3).

    ``candidates`` is deterministically ordered (by pair) so a replay/trace is stable regardless
    of generator/row iteration order (§10). One :class:`Candidate` per ``(evidence, hypothesis)``
    pair — generators that proposed the same pair are merged, their sources/entities unioned.
    """

    candidates: tuple[Candidate, ...] = ()

    def __len__(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True)
class InvolvesRow:
    """One active ``INVOLVES`` edge as read from AGE: a reasoning node references an entity (§10).

    ``node`` is a ``Fact``/``Conclusion``/``Hypothesis`` id, ``entity`` an ``Actor``/``Object`` id,
    ``role`` the entity's role in the claim (``subject``/``object``/``instrument``; the privileged
    ``subject`` anchors derived abstraction level, §14 — carried for the coarse-to-fine seam,
    unused by the role-agnostic structural prior).
    """

    node: NodeId
    entity: NodeId
    role: str | None = None


def structural_entity_candidates(
    *,
    hypotheses: Iterable[NodeId],
    evidence: Iterable[NodeId],
    involves: Iterable[InvolvesRow],
) -> list[Candidate]:
    """Stage 1 (§5.1): pair every hypothesis with evidence sharing one of its ``INVOLVES`` entities.

    Pure (DB-free). An ``(evidence, hypothesis)`` pair is a candidate iff both nodes reference a
    common ``Actor``/``Object`` (role-agnostic co-occurrence — the near-free structural prior).
    Only ``involves`` rows whose ``node`` is in the given ``hypotheses``/``evidence`` sets count,
    so the caller's active-box scoping (§9) carries through; an entity links a hypothesis to
    *itself* or to another hypothesis is ignored (candidates run evidence → hypothesis only).

    Each emitted :class:`Candidate` carries the **full set** of entities the pair shares (so a
    pair sharing two entities is one candidate, its rationale complete), tagged
    :attr:`CandidateSource.STRUCTURAL_ENTITY`.
    """
    hyp_set = set(hypotheses)
    ev_set = set(evidence)

    # entity -> the hypotheses / evidence that reference it (active nodes only)
    by_entity_hyp: dict[NodeId, set[NodeId]] = {}
    by_entity_ev: dict[NodeId, set[NodeId]] = {}
    for row in involves:
        if row.node in hyp_set:
            by_entity_hyp.setdefault(row.entity, set()).add(row.node)
        elif row.node in ev_set:
            by_entity_ev.setdefault(row.entity, set()).add(row.node)

    # (evidence, hypothesis) -> the entities they share — accumulate then emit one candidate each.
    shared: dict[tuple[NodeId, NodeId], set[NodeId]] = {}
    for entity, hyps in by_entity_hyp.items():
        evs = by_entity_ev.get(entity)
        if not evs:
            continue
        for h in hyps:
            for e in evs:
                shared.setdefault((e, h), set()).add(entity)

    return [
        Candidate(
            evidence=e,
            hypothesis=h,
            sources=frozenset({CandidateSource.STRUCTURAL_ENTITY}),
            shared_entities=frozenset(entities),
        )
        for (e, h), entities in shared.items()
    ]


def funnel(
    *generators: Iterable[Candidate],
    strategy: FunnelStrategy = DEFAULT_STRATEGY,
) -> CandidatePool:
    """Combine the cheap generators' candidates into one deduped, deterministic pool (§5.1).

    Written once, generic over :class:`FunnelStrategy` (not branched per generator): each
    ``generators`` argument is one stage's output, merged by ``(evidence, hypothesis)`` pair with
    sources/entities **unioned** (a pair two stages found is one candidate carrying both sources).

    - :attr:`FunnelStrategy.UNION` (default) keeps every pair any generator proposed — recall-first
      (§5.1); the dissimilar refuter the embedding stage misses but the structural stage catches
      survives.
    - :attr:`FunnelStrategy.INTERSECT` keeps only pairs **every** non-empty generator proposed (a
      precision pre-filter; drops the dissimilar refuter — never the candidate-generation default).

    Output is sorted by pair so the trace is stable (§10).
    """
    # Merge all candidates by pair, unioning provenance, and track which generators proposed each.
    merged: dict[tuple[NodeId, NodeId], Candidate] = {}
    proposed_by: dict[tuple[NodeId, NodeId], int] = {}
    n_generators = 0
    for gen in generators:
        seen_this_gen: set[tuple[NodeId, NodeId]] = set()
        produced = False
        for cand in gen:
            produced = True
            key = cand.key
            prev = merged.get(key)
            if prev is None:
                merged[key] = cand
            else:
                merged[key] = Candidate(
                    evidence=key[0],
                    hypothesis=key[1],
                    sources=prev.sources | cand.sources,
                    shared_entities=prev.shared_entities | cand.shared_entities,
                )
            if key not in seen_this_gen:
                seen_this_gen.add(key)
                proposed_by[key] = proposed_by.get(key, 0) + 1
        if produced:
            n_generators += 1

    if strategy is FunnelStrategy.INTERSECT and n_generators > 1:
        # Keep only pairs every non-empty generator proposed (precision pre-filter, §5.1).
        merged = {k: v for k, v in merged.items() if proposed_by.get(k, 0) == n_generators}

    candidates = tuple(sorted(merged.values(), key=lambda c: c.key))
    return CandidatePool(candidates=candidates)


class CandidateGenerationAdapter:
    """Generates candidates from the active AGE subgraph (§5.1) — the boundary, DB-bound half.

    DB-free to construct; the reads happen in the ``async`` methods (lazy ``iknos.db.age``
    import). Stateless — a full current-state read per call, like the QBAF/derivation adapters.
    Reuses the shared ``load_active_box_ids`` / ``load_reasoning_nodes`` / ``load_hypothesis_ids``
    reads, so the active-subgraph definition is single-sourced (§9, §10).
    """

    async def _load_involves(self, session: object) -> list[InvolvesRow]:
        """All current ``INVOLVES`` edges between current nodes, with the entity ``role`` (§10).

        Both endpoints must be bitemporally current (``valid_to IS NULL``) — an edge to a
        retracted entity links nothing. Active-box scoping is applied in :meth:`generate` against
        the shared active-node universe (the entity's box is not re-read here).
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        rows = await execute_cypher(
            session,  # type: ignore[arg-type]
            "MATCH (n)-[r:INVOLVES]->(e) "
            "WHERE n.valid_to IS NULL AND e.valid_to IS NULL "
            "RETURN n.id, e.id, r.role",
            returns="nid agtype, eid agtype, role agtype",
        )
        return [
            InvolvesRow(node=unquote_agtype(nid), entity=unquote_agtype(eid), role=_opt_str(role))
            for nid, eid, role in rows
        ]

    async def generate(
        self,
        session: object,
        *,
        strategy: FunnelStrategy = DEFAULT_STRATEGY,
    ) -> CandidatePool:
        """Read the active subgraph and run the funnel — the read path G4.3 consumes.

        Partitions the active reasoning nodes into hypotheses (the targets) and evidence
        (everything else — ``Fact``/``Conclusion``), scopes the ``INVOLVES`` rows to that active
        universe, runs stage 1, and folds it through :func:`funnel`. (Embedding-NN / coarse-to-fine
        are the slice-2 generators that union in here without changing the contract.)
        """
        active_box_ids = await load_active_box_ids(session)
        nodes = await load_reasoning_nodes(session)
        hyp_ids = await load_hypothesis_ids(session)
        involves = await self._load_involves(session)

        active_ids = {n.id for n in nodes if active_box_ids is None or n.box in active_box_ids}
        hypotheses = active_ids & hyp_ids
        evidence = active_ids - hyp_ids
        involves_active = [r for r in involves if r.node in active_ids]

        structural = structural_entity_candidates(
            hypotheses=hypotheses, evidence=evidence, involves=involves_active
        )
        return funnel(structural, strategy=strategy)


def _opt_str(v: object) -> str | None:
    """Parse an agtype scalar that may be SQL/agtype null into ``str | None`` (mirrors the
    derivation/QBAF adapters' null-tolerant parse; an ``INVOLVES`` ``role`` may be absent)."""
    if v is None or str(v) == "null":
        return None
    from iknos.db.age import unquote_agtype

    return unquote_agtype(v)
