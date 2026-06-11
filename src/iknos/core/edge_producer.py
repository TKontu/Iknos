"""Phase 4 evidential-edge producer (G4.3 slice 3; architecture §5, §8, §9, §10, §10.1).

The data-bound increment that closes the §8 edge-judgment pipeline: it reads the G4.2 candidate
pool out of the active AGE subgraph, resolves each node's claim text and each evidence node's
source credibility, calls the blind/randomized/multi-sample
:class:`~iknos.core.edge_judge.EdgeJudge`
(G4.3 slice 2), and **writes the surviving ``SUPPORTS``/``REFUTES`` edges** carrying ``sign`` /
``strength`` / ``significance`` plus a provenance :class:`~iknos.db.orm.Action` (§10.1). It is the
Phase-4 analogue of how the propositionizer wraps the ``Verifier`` (and of ``derive.py`` wrapping
the Layer A/B valuation): the LLM/pure cores are DB-free; this is the boundary that feeds them the
graph and persists their verdict. The consuming end of the contract is already in place — the QBAF
adapter (G4.4) reads exactly these edges back into the gradual-semantics engine.

**Pure / DB split** (mirrors ``qbaf_adapter.py`` / ``candidates.py``). The value types, the
significance policy, the evidence grouping, the edge-property flattening and the per-hypothesis
*write plan* (:func:`plan_hypothesis`) are all DB-free and unit-testable with hand-built rows; the
:class:`EdgeProducer` does the AGE reads/writes in its ``async`` methods behind a lazy
``iknos.db.age`` import, judging hypotheses concurrently and persisting their plans serially in one
transaction (the propositionizer's concurrent-infer / serial-persist shape).

**The three edge quantities, kept separate (§3.1, §8, §9 — "three separate quantities, never
merged").** A ``SUPPORTS``/``REFUTES`` edge carries:

- **``sign``** — the categorical direction (``SUPPORTS`` vs ``REFUTES``), decided first and
  structurally (the *edge type*), exactly as :class:`~iknos.core.qbaf_adapter.QbafAdapter` reads it
  back off the label. The judge owns the classification; the graph owns its persistence.
- **``strength``** — the calibrated connection weight ∈ [0, 1]: *how strongly this evidence bears
  on this hypothesis* (§8). The projected probability of the judge's multi-sample subjective-logic
  opinion — **never a raw LLM number** (§10), and (the reconciliation below) **not** discounted by
  source credibility: strength is the pure connection judgment, a property of the *relationship*.
- **``significance``** — *weight of the evidence if true* ∈ [0, 1] (§9): largely inherited from the
  evidence node's source/tier, barely dependent on the LLM. This is where the **credibility** term
  lives (§9.1, architecture §9: "conditional credibility … feeds the edge ``significance`` — it is
  the *credibility* term in the faithfulness/credibility/strength separation").

**Reconciling the §8/§9 credibility routing (recorded so it is not re-litigated).** G4.3 slice 2's
plan was to route a source's ``effective_credibility`` into the judge's subjective-logic *trust
discount*, i.e. into the **strength**. But the architecture (§3.1/§8/§9, and ``types/edges.py``'s
``EvidentialEdge`` docstring) holds ``strength`` and ``significance`` and ``credibility`` to be
**three separate quantities, never merged** — and puts credibility into ``significance``, leaving
``strength`` the pure connection judgment. So this producer keeps the architecture's separation:
the judge is called at the **identity reliability** (``1.0``), so its strength stays the unmodulated
connection weight, and the evidence node's ``effective_credibility`` is routed into
:func:`edge_significance` instead. The judge's ``reliability`` discount is **retained as a seam**
(:attr:`~iknos.core.edge_judge.JudgeEvidence.reliability`) for a deliberately decorrelated
sub-domain, but is not the default path — merging credibility into strength is exactly what the
separation forbids.

**The active subgraph + the gate boundary (§7.2, §10, §12).** Candidates are generated over the
bitemporally-current, active-box subgraph (the G4.2 adapter's discipline). The producer writes every
surviving directional edge, **including** ``REFUTES`` — but a *persisted hypothesis-state flip* to
``refuted`` is **not** this layer's call: that is gated on the §7.2 ensemble (multi-sample LLM +
symbolic + temporal agreement, G4.5), which consumes the ``sign_stable=False`` findings this
producer surfaces (and persists on the edge) before authorising a flip. This producer writes
edges; it does
not adjudicate hypothesis state (that is the QBAF, G4.4) nor authorise refutation (that is the gate,
G4.5).
"""

import asyncio
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from iknos.core.candidates import DEFAULT_K, CandidateGenerationAdapter, CandidatePool
from iknos.core.derivation_adapter import (
    REASONING_LABELS,
)
from iknos.core.edge_judge import (
    EdgeJudge,
    EdgeJudgment,
    HypothesisJudgment,
    JudgeEvidence,
)
from iknos.core.quarantine import DEFAULT_QUARANTINE, QuarantinePolicy, is_quarantined
from iknos.core.truth_maintenance import NodeId
from iknos.types.edges import EdgeSign
from iknos.types.nodes import Tier

# The actor + action_type stamped on every judgment Action (§10.1) — the audit handle a reader
# greps for to find "which edges did the edge judge write, and from what samples".
PRODUCER_ACTOR = "edge-judge"
PRODUCER_ACTION_TYPE = "judge"


@dataclass(frozen=True)
class SignificancePolicy:
    """How an evidence node's metadata becomes an edge's ``significance`` ∈ [0, 1] (§9).

    ``significance`` is *weight of the evidence if true* — largely inherited from the evidence
    node's **source/tier**, barely dependent on the LLM (§8/§9). It is **derived, never an LLM
    number** and computed by exactly this one policy, so calibration (G4.6) re-points it without
    touching the producer — the same *policy-as-swappable-data* discipline as the §11.2 verdict
    bands (:data:`~iknos.types.intentional._BAND_LOWER_BOUNDS`) and the Layer-B / QBAF / fusion
    decision values.

    Two inputs compose multiplicatively (both ∈ [0, 1], so the product is too):

    - **``tier_weight``** — a per-:class:`~iknos.types.nodes.Tier` weight (an authoritative
      reference fact may weigh differently from a working derivation). Defaults to **uniform
      ``1.0``** — the honest placeholder: a tier-differentiated weighting is a genuine calibration
      question (G4.6), so the MVP leaves it identity rather than inventing an unjustified ordering,
      while still threading ``tier`` through so the seam is real, not absent.
    - the evidence's **``effective_credibility``** (§9.1, the credibility term) — passed in at
      compute time, defaulting to ``1.0`` when the source chain is incomplete (a Conclusion with no
      box reliability, or an un-evidenced node): credibility is *undefined, not zero* when the chain
      is incomplete (``core/credibility.py``), so an unknown source is not silently penalised.

    ``default_tier_weight`` covers a tier absent from the map (additive Tier members do not break
    the policy).
    """

    tier_weight: Mapping[Tier, float] = field(default_factory=dict)
    default_tier_weight: float = 1.0

    def weight_for(self, tier: Tier | None) -> float:
        """The tier weight, falling back to ``default_tier_weight`` (and for an unknown tier)."""
        if tier is None:
            return self.default_tier_weight
        return self.tier_weight.get(tier, self.default_tier_weight)


DEFAULT_SIGNIFICANCE = SignificancePolicy()
"""Uniform tier weighting (significance = the §9.1 credibility term) — the calibration-target MVP.

Tier-differentiated significance is deferred to the validation gate (G4.6), where it is calibrated
against measured outcomes rather than guessed here; until then significance *is* the evidence's
effective credibility, the one §9 signal we can compute today."""


def edge_significance(
    policy: SignificancePolicy, tier: Tier | None, credibility: float | None
) -> float:
    """An evidence node's edge ``significance`` ∈ [0, 1] from its tier + source credibility (§9).

    ``significance = tier_weight(tier) · credibility`` (credibility defaulting to ``1.0`` when the
    source chain is incomplete, see :class:`SignificancePolicy`), clamped to [0, 1] for safety even
    though both factors are already bounded. Pure — the producer computes it per surviving edge.
    """
    cred = 1.0 if credibility is None else credibility
    if not 0.0 <= cred <= 1.0:
        raise ValueError(f"credibility must be in [0, 1], got {credibility!r}")
    return max(0.0, min(1.0, policy.weight_for(tier) * cred))


@dataclass(frozen=True)
class NodeMeta:
    """One active reasoning node's adjudication-relevant metadata, read from AGE.

    ``statement`` is the canonical claim text the judge reads (always present on a
    ``Fact``/``Conclusion``/``Hypothesis``, §10 — the robust claim source, vs the finer
    ``EVIDENCED_BY`` → ``Proposition.text`` provenance which is empty for an un-propositionized
    hypothesis stub). ``tier`` feeds :func:`edge_significance`; ``box`` is the node's owning box
    (the written edge inherits its **target** hypothesis's box).

    ``provisional`` is the node's *own* ``provisional`` property (§3.1) — present on a
    :class:`~iknos.types.nodes.Conclusion` (``True`` for an ``induce``d/defeasible conclusion,
    ``False`` for a ``deduce``d one), ``None`` on a ``Fact``/``Hypothesis`` which carry none. A
    base Fact's provisional status lives on its source ``Proposition`` instead, resolved by the
    ``EVIDENCED_BY`` walk in :meth:`EdgeProducer._load_provisional`; the two combine into the
    quarantine input (§3.1, G2.9).
    """

    statement: str
    tier: Tier | None
    box: str | None
    provisional: bool | None = None


@dataclass(frozen=True)
class EdgeWrite:
    """One planned ``SUPPORTS``/``REFUTES`` edge write — DB-free, so the plan is unit-testable.

    ``label`` is the AGE relationship type (``"SUPPORTS"``/``"REFUTES"``, the structural sign);
    ``props`` the flattened edge properties (:func:`evidential_edge_props`). :class:`EdgeProducer`
    turns each into one ``merge_edge`` call.
    """

    src_id: NodeId
    dst_id: NodeId
    label: str
    props: dict[str, Any]


@dataclass(frozen=True)
class PlannedAction:
    """The provenance :class:`~iknos.db.orm.Action` for one hypothesis's judgment (§10.1), DB-free.

    Mirrors :func:`~iknos.provenance.action_log.record_action`'s keyword contract so
    :class:`EdgeProducer` can write it verbatim. The raw panel tally (votes, abstentions, the
    dropped-irrelevant pairs, the schema/prompt shas) lives here — the auditable record that an edge
    was judged from *these* samples under *this* pipeline, separate from the graph-queryable values
    on the edge itself.
    """

    inputs: dict[str, Any]
    outputs: dict[str, Any]
    model: str | None
    sampling: dict[str, Any]
    actor: str = PRODUCER_ACTOR
    action_type: str = PRODUCER_ACTION_TYPE


@dataclass(frozen=True)
class HypothesisPlan:
    """The complete write plan for one hypothesis: its edges + its provenance Action (DB-free)."""

    hypothesis: NodeId
    edges: tuple[EdgeWrite, ...]
    action: PlannedAction


def evidential_edge_props(
    *,
    box: str | None,
    sign: EdgeSign,
    strength: float,
    significance: float,
    sign_stable: bool,
    quarantined: bool,
    now: datetime,
) -> dict[str, Any]:
    """Flatten one evidential edge to AGE properties — the canonical write contract (cf.
    ``derive.derivation_edge_props``).

    Carries the three §8/§9 quantities (``sign``/``strength``/``significance``), the
    ``sign_stable`` finding (so the §7.2 ensemble gate can *graph-query* unstable directional edges
    before authorising a ``refuted`` flip — a first-class signal, not buried only in the Action),
    the ``quarantined`` flag (§3.1, G2.9 — ``True`` when a *provisional* source drives this
    high-stakes move; the QBAF adapter drops a quarantined edge so it does not overturn a hypothesis
    until the source is confirmed, but the edge persists so it is auditable and lifts on
    re-judgment), and bitemporal fields stamped **open** (``valid_to``/``event_time`` null), so a
    retraction stamps ``valid_to`` and the QBAF adapter's current-state filter drops the edge (§10).
    ``sign`` is stored redundantly with the label for ``EvidentialEdge``-schema fidelity; the QBAF
    adapter takes direction from the label, the canonical source.
    """
    return {
        "box": box,
        "sign": str(sign),
        "strength": strength,
        "significance": significance,
        "sign_stable": sign_stable,
        "quarantined": quarantined,
        "event_time": None,
        "ingested_at": now.isoformat(),
        "valid_from": now.isoformat(),
        "valid_to": None,
    }


def build_evidence(
    pool: CandidatePool, node_meta: Mapping[NodeId, NodeMeta]
) -> dict[NodeId, tuple[str, list[JudgeEvidence]]]:
    """Group the candidate pool into per-hypothesis ``(hypothesis_text, [JudgeEvidence])`` — pure.

    Each hypothesis with a resolvable ``statement`` collects its candidate evidence (those whose
    ``statement`` resolved too); a node missing from ``node_meta`` (no text) is silently dropped —
    nothing to judge. The judge is fed the **identity reliability** (``1.0``): per the module
    reconciliation, source credibility is routed into ``significance`` at write time, *not* into the
    judge's strength discount. Evidence is sorted by id so the panel presentation (and its
    permutation seed) is replayable (§10) regardless of pool/row order.
    """
    by_hyp: dict[NodeId, list[JudgeEvidence]] = {}
    for cand in pool.candidates:
        if cand.hypothesis not in node_meta:
            continue
        ev_meta = node_meta.get(cand.evidence)
        if ev_meta is None:
            continue
        by_hyp.setdefault(cand.hypothesis, []).append(
            JudgeEvidence(id=cand.evidence, text=ev_meta.statement)
        )

    grouped: dict[NodeId, tuple[str, list[JudgeEvidence]]] = {}
    for hyp, evidence in by_hyp.items():
        grouped[hyp] = (
            node_meta[hyp].statement,
            sorted(evidence, key=lambda e: e.id),
        )
    return grouped


def plan_hypothesis(
    judgment: HypothesisJudgment,
    *,
    node_meta: Mapping[NodeId, NodeMeta],
    credibility: Mapping[NodeId, float | None],
    provisional: Mapping[NodeId, bool],
    policy: SignificancePolicy,
    quarantine_policy: QuarantinePolicy,
    now: datetime,
    model: str | None,
    sampling: dict[str, Any],
    prompt_sha: str,
    schema_sha: str,
    schema_version: int,
) -> HypothesisPlan:
    """Turn one judge verdict into its edge writes + provenance Action — pure, the producer's meat.

    For each surviving :class:`~iknos.core.edge_judge.EdgeJudgment`: ``significance`` is computed
    from the **evidence** node's tier + credibility (§9, :func:`edge_significance`), the edge is
    planned with the calibrated ``strength`` (§8) and the structural ``sign`` (the label), the
    §3.1 ``quarantined`` flag is decided from the **evidence** node's provisional status +
    ``quarantine_policy`` (:func:`~iknos.core.quarantine.is_quarantined` — a provisional source may
    not drive a ``REFUTES``), and the panel tally is folded into the Action provenance (§10.1) —
    including the dropped-``irrelevant`` pairs (auditable as *considered and rejected*, not silently
    missing) and the ``prompt_sha``/``schema_sha``/``schema_version`` (so a re-judgment under a
    changed pipeline is detectable). The edge inherits the **target hypothesis's** box.
    """
    hyp_box = node_meta[judgment.hypothesis].box if judgment.hypothesis in node_meta else None

    edges: list[EdgeWrite] = []
    edge_provenance: list[dict[str, Any]] = []
    for j in judgment.judgments:
        ev_meta = node_meta.get(j.evidence)
        significance = edge_significance(
            policy,
            ev_meta.tier if ev_meta is not None else None,
            credibility.get(j.evidence),
        )
        quarantined = is_quarantined(
            j.sign, provisional.get(j.evidence, False), policy=quarantine_policy
        )
        props = evidential_edge_props(
            box=hyp_box,
            sign=j.sign,
            strength=j.strength,
            significance=significance,
            sign_stable=j.sign_stable,
            quarantined=quarantined,
            now=now,
        )
        edges.append(
            EdgeWrite(
                src_id=j.evidence,
                dst_id=judgment.hypothesis,
                label=str(j.sign).upper(),
                props=props,
            )
        )
        edge_provenance.append(_edge_audit(j, significance, quarantined))

    action = PlannedAction(
        inputs={
            "hypothesis": judgment.hypothesis,
            "candidates": sorted(
                {j.evidence for j in judgment.judgments} | set(judgment.irrelevant)
            ),
            "prompt_sha": prompt_sha,
            "schema_sha": schema_sha,
            "schema_version": schema_version,
        },
        outputs={
            "edges": [f"{e.src_id}->{e.dst_id}" for e in edges],
            "judgments": edge_provenance,
            "dropped_irrelevant": list(judgment.irrelevant),
        },
        model=model,
        sampling=sampling,
    )
    return HypothesisPlan(hypothesis=judgment.hypothesis, edges=tuple(edges), action=action)


def _edge_audit(j: EdgeJudgment, significance: float, quarantined: bool) -> dict[str, Any]:
    """One edge's raw judgment record for the Action (§10.1) — the votes behind the number."""
    return {
        "evidence": j.evidence,
        "sign": str(j.sign),
        "strength": j.strength,
        "significance": significance,
        "positive": j.positive,
        "negative": j.negative,
        "abstained": j.abstained,
        "n_samples": j.n_samples,
        "sign_stable": j.sign_stable,
        "quarantined": quarantined,
    }


@dataclass(frozen=True)
class ProducedEdge:
    """One persisted evidential edge — the producer's externally-visible result row."""

    evidence: NodeId
    hypothesis: NodeId
    sign: EdgeSign
    strength: float
    significance: float
    sign_stable: bool
    quarantined: bool = False


@dataclass(frozen=True)
class EdgeProductionResult:
    """The outcome of :meth:`EdgeProducer.produce` (§8, §13).

    ``edges`` are the persisted survivors; ``dropped`` the ``(evidence, hypothesis)`` pairs the
    panel judged ``irrelevant`` (recall→precision handoff, §5.1); ``unstable`` the persisted edges
    whose panel split *direction* (``sign_stable=False``) — the §13 findings the §7.2 ensemble gate
    (G4.5)
    must clear before a ``refuted`` flip, surfaced not smoothed; ``quarantined`` the persisted edges
    a *provisional* source drove into a high-stakes move (§3.1, G2.9) — written for audit but
    dropped by the QBAF adapter so they do not overturn a hypothesis until the source is confirmed.
    ``action_ids`` are the provenance Actions written (one per judged hypothesis).
    """

    edges: tuple[ProducedEdge, ...] = ()
    dropped: tuple[tuple[NodeId, NodeId], ...] = ()
    action_ids: tuple[uuid.UUID, ...] = ()

    @property
    def unstable(self) -> tuple[ProducedEdge, ...]:
        """The persisted edges with an unstable sign — the gate's input (§7.2, §13)."""
        return tuple(e for e in self.edges if not e.sign_stable)

    @property
    def quarantined(self) -> tuple[ProducedEdge, ...]:
        """The persisted edges a provisional source drove into a high-stakes move (§3.1, G2.9).

        The QBAF adapter drops these from the framework (they do not drive a hypothesis's state);
        surfaced here so the expert-triage queue (Phase 7) can route the provisional source for
        confirmation — the value-of-information item that, once confirmed, lifts the quarantine.
        """
        return tuple(e for e in self.edges if e.quarantined)

    @property
    def is_finding(self) -> bool:
        """Whether any persisted edge has an unstable sign (a §13 finding)."""
        return any(not e.sign_stable for e in self.edges)


class EdgeProducer:
    """Reads candidates from active AGE, judges them, and persists evidential edges (§8, G4.3 s3).

    DB-free to construct (it carries an :class:`~iknos.core.edge_judge.EdgeJudge`, a candidate
    adapter and a :class:`SignificancePolicy`); the reads/writes happen in :meth:`produce`.
    Stateless across calls — a full read, judge, write per call, mirroring the QBAF / candidate
    adapters and the propositionizer's concurrent-infer / serial-persist shape.
    """

    def __init__(
        self,
        judge: EdgeJudge,
        *,
        candidates: CandidateGenerationAdapter | None = None,
        policy: SignificancePolicy = DEFAULT_SIGNIFICANCE,
        quarantine_policy: QuarantinePolicy = DEFAULT_QUARANTINE,
        concurrency: int = 4,
    ) -> None:
        self.judge = judge
        self.candidates = candidates or CandidateGenerationAdapter()
        self.policy = policy
        self.quarantine_policy = quarantine_policy
        self.concurrency = concurrency

    async def _load_node_meta(self, session: object) -> dict[NodeId, NodeMeta]:
        """All current reasoning nodes' ``statement`` + ``tier`` + ``box`` (one query per label).

        One query per :data:`~iknos.core.derivation_adapter.REASONING_LABELS` label (AGE matches a
        single label per pattern, as in ``load_reasoning_nodes``); only bitemporally-current
        (``valid_to IS NULL``) nodes. The active-box scope is applied by candidate generation
        upstream — this read just resolves text/metadata for any node a candidate references.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        meta: dict[NodeId, NodeMeta] = {}
        for label in REASONING_LABELS:
            rows = await execute_cypher(
                session,  # type: ignore[arg-type]
                f"MATCH (n:{label}) WHERE n.valid_to IS NULL "
                "RETURN n.id, n.statement, n.tier, n.box, n.provisional",
                returns="nid agtype, statement agtype, tier agtype, box agtype, provisional agtype",
            )
            for nid, statement, tier, box, provisional in rows:
                meta[unquote_agtype(nid)] = NodeMeta(
                    statement=_opt_str(statement) or "",
                    tier=_opt_tier(tier),
                    box=_opt_str(box),
                    provisional=_opt_bool(provisional),
                )
        return meta

    async def _load_provisional(
        self, session: object, evidence_ids: Iterable[NodeId], node_meta: Mapping[NodeId, NodeMeta]
    ) -> dict[NodeId, bool]:
        """Each evidence node's **provisional** status (§3.1) — the quarantine input, OR-folded.

        A node is provisional if *either*:

        - its own ``provisional`` property is ``True`` — a defeasible ``induce``d
          :class:`~iknos.types.nodes.Conclusion` (read in :meth:`_load_node_meta`); or
        - it is a base ``Fact`` whose source ``Proposition`` is provisional — the perception-layer
          gate (low faithfulness / ambiguous binding / polarity-unstable, §3.1). A ``Fact`` carries
          no ``provisional`` of its own, so this walks ``Fact -[:EVIDENCED_BY]-> Proposition`` and
          OR-folds ``Proposition.provisional`` over its (normally one) sources.

        A ``null`` provisional reads as ``False`` everywhere — quarantine fires only on a *positive*
        provisional signal, never on its absence (the perception layer may simply not have judged
        the proposition yet). Returns a total map over ``evidence_ids``.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        out: dict[NodeId, bool] = {
            eid: bool(node_meta[eid].provisional) if eid in node_meta else False
            for eid in evidence_ids
        }

        # Fact -> source Proposition: the perception-layer provisional gate a Fact inherits (§3.1).
        # One query over all current Facts (investigation scale, as the QBAF/candidate reads); the
        # result is filtered to evidence_ids and OR-folded so a Fact with several propositions is
        # provisional if any is.
        rows = await execute_cypher(
            session,  # type: ignore[arg-type]
            "MATCH (f:Fact)-[:EVIDENCED_BY]->(p:Proposition) WHERE f.valid_to IS NULL "
            "RETURN f.id, p.provisional",
            returns="fid agtype, provisional agtype",
        )
        for fid, provisional in rows:
            nid = unquote_agtype(fid)
            if nid in out and _opt_bool(provisional) is True:
                out[nid] = True
        return out

    async def _load_credibility(
        self, session: object, evidence_ids: Iterable[NodeId]
    ) -> dict[NodeId, float | None]:
        """Each evidence node's ``effective_credibility`` ∈ [0, 1] (§9.1) — the significance input.

        Delegates to ``core/credibility.effective_credibility_of`` (the single derived-not-stored
        implementation), so the §9.1 credibility used at the edge layer cannot diverge from the
        proposition layer's. Returns ``None`` for a node whose source chain is incomplete (a
        Conclusion with no box reliability, an un-evidenced node) — :func:`edge_significance` reads
        that as the identity ``1.0`` (undefined, not zero).
        """
        from iknos.core.credibility import effective_credibility_of

        out: dict[NodeId, float | None] = {}
        for eid in evidence_ids:
            try:
                out[eid] = await effective_credibility_of(session, uuid.UUID(eid))  # type: ignore[arg-type]
            except ValueError:
                # A non-UUID id (defensive) contributes no credibility rather than aborting the run.
                out[eid] = None
        return out

    async def produce(
        self,
        session: object,
        *,
        k: int = DEFAULT_K,
    ) -> EdgeProductionResult:
        """Generate candidates, judge them, and write the surviving evidential edges (one txn).

        Concurrent-infer / serial-persist (the propositionizer's shape):

        1. **Read** the candidate pool (G4.2 funnel over the active subgraph) + node text/tier/box +
           per-evidence credibility.
        2. **Judge** each hypothesis's candidate set concurrently (the blind/randomized/multi-sample
           §8 panel), bounded by a shared semaphore so the whole run shares one LLM budget.
        3. **Plan** each verdict's edge writes + Action purely (:func:`plan_hypothesis`), then
           **persist** them serially and commit atomically — the writes are the only mutation, so a
           single transaction keeps the graph consistent if any write fails.

        Returns the persisted edges, the dropped-irrelevant pairs, and the Action ids; the
        sign-unstable edges are surfaced on the result (``unstable`` / ``is_finding``) for the §7.2
        gate (G4.5).
        """
        from iknos.db.age import merge_edge
        from iknos.provenance.action_log import record_action

        pool = await self.candidates.generate(session, k=k)
        if not pool.candidates:
            return EdgeProductionResult()

        node_meta = await self._load_node_meta(session)
        grouped = build_evidence(pool, node_meta)
        if not grouped:
            return EdgeProductionResult()

        evidence_ids = {e.id for _hyp, (_text, evs) in grouped.items() for e in evs}
        credibility = await self._load_credibility(session, evidence_ids)
        provisional = await self._load_provisional(session, evidence_ids, node_meta)

        # Phase 2 — judge every hypothesis's set concurrently under one shared LLM budget.
        sem = asyncio.Semaphore(self.concurrency)
        judgments: list[HypothesisJudgment] = await asyncio.gather(
            *(
                self.judge.judge_hypothesis(hyp, text, evidence, sem=sem)
                for hyp, (text, evidence) in sorted(grouped.items())
            )
        )

        # Phase 3 — plan (pure) then persist serially, one transaction.
        now = datetime.now(UTC)
        produced: list[ProducedEdge] = []
        dropped: list[tuple[NodeId, NodeId]] = []
        action_ids: list[uuid.UUID] = []
        for judgment in judgments:
            dropped.extend((ev, judgment.hypothesis) for ev in judgment.irrelevant)
            if not judgment.judgments:
                continue  # nothing survived for this hypothesis — no edges, no Action
            plan = plan_hypothesis(
                judgment,
                node_meta=node_meta,
                credibility=credibility,
                provisional=provisional,
                policy=self.policy,
                quarantine_policy=self.quarantine_policy,
                now=now,
                model=getattr(self.judge.llm, "model", None),
                sampling=self.judge.sampling,
                prompt_sha=self.judge.prompt_sha(),
                schema_sha=self.judge.schema_sha(),
                schema_version=self.judge.SCHEMA_VERSION,
            )
            for edge in plan.edges:
                await merge_edge(
                    session,  # type: ignore[arg-type]
                    src_id=edge.src_id,
                    dst_id=edge.dst_id,
                    label=edge.label,
                    props=edge.props,
                )
            action_ids.append(
                await record_action(
                    session,  # type: ignore[arg-type]
                    actor=plan.action.actor,
                    action_type=plan.action.action_type,
                    inputs=plan.action.inputs,
                    outputs=plan.action.outputs,
                    model=plan.action.model,
                    sampling=plan.action.sampling,
                )
            )
            for j in judgment.judgments:
                ev_meta = node_meta.get(j.evidence)
                produced.append(
                    ProducedEdge(
                        evidence=j.evidence,
                        hypothesis=judgment.hypothesis,
                        sign=j.sign,
                        strength=j.strength,
                        significance=edge_significance(
                            self.policy,
                            ev_meta.tier if ev_meta is not None else None,
                            credibility.get(j.evidence),
                        ),
                        sign_stable=j.sign_stable,
                        quarantined=is_quarantined(
                            j.sign,
                            provisional.get(j.evidence, False),
                            policy=self.quarantine_policy,
                        ),
                    )
                )

        await session.commit()  # type: ignore[attr-defined]
        return EdgeProductionResult(
            edges=tuple(produced),
            dropped=tuple(dropped),
            action_ids=tuple(action_ids),
        )


def _opt_str(v: object) -> str | None:
    """Parse an agtype scalar that may be SQL/agtype null into ``str | None`` (mirrors the
    derivation/QBAF/candidate adapters' null-tolerant parse)."""
    if v is None or str(v) == "null":
        return None
    from iknos.db.age import unquote_agtype

    return unquote_agtype(v)


def _opt_bool(v: object) -> bool | None:
    """Parse an agtype boolean that may be SQL/agtype null into ``bool | None``.

    AGE renders a boolean property unquoted (``true``/``false``) and a missing one as ``null``;
    ``None`` / ``"null"`` → ``None`` (the §3.1 *undecided* provisional state — neither provisional
    nor confirmed). Anything else is compared against the literal ``true``."""
    if v is None or str(v) == "null":
        return None
    return str(v) == "true"


def _opt_tier(v: object) -> Tier | None:
    """Parse an agtype ``tier`` property into a :class:`~iknos.types.nodes.Tier`, null-tolerant.

    An unrecognised value (a graph written under a newer vocabulary) yields ``None`` rather than
    raising — significance falls back to the policy's default weight, never aborting a run on a
    metadata surprise."""
    s = _opt_str(v)
    if s is None:
        return None
    try:
        return Tier(s)
    except ValueError:
        return None
