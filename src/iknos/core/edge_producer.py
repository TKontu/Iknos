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

**The §3.1 quarantine gate (V7, invariant).** This is the live ``SUPPORTS``/``REFUTES`` creation
site, so the §3.1 rule lands here: a **provisional** source (a Fact/Conclusion inheriting the union
of ``provisional_reasons`` over the ``Proposition``s it is ``EVIDENCED_BY``, R8) may not drive a
**high-stakes** move — a ``REFUTES`` or a *sole-support* ``SUPPORTS`` (:func:`edge_stakes`). Before
each edge is planned, :func:`~iknos.core.quarantine.assert_not_quarantined` is consulted; a
quarantined edge is **dropped from the plan** (never persisted) and recorded as a
:class:`QuarantineRecord` on the result + the ``Action``'s ``outputs.quarantined`` — a triage
signal, **never a silent skip and never an abort** (other edges and hypotheses are unaffected). The
gate is
at the *write*, not the candidate/judge stage: the judge still sees the evidence (the panel is blind
to provenance). Enforcement is the producer's; the pure decision is ``core/quarantine`` (R9) and the
reason vocabulary is R8's ``ProvisionalReason``. The quarantine lifts non-destructively when the
source is confirmed (its reasons clear and a re-judgment re-plans the edge).
"""

import asyncio
import json
import logging
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
from iknos.core.quarantine import QuarantinedPropositionError, Stakes, assert_not_quarantined
from iknos.core.truth_maintenance import NodeId
from iknos.types.edges import EdgeSign
from iknos.types.nodes import Tier

logger = logging.getLogger(__name__)

# The actor + action_type stamped on every judgment Action (§10.1) — the audit handle a reader
# greps for to find "which edges did the edge judge write, and from what samples".
PRODUCER_ACTOR = "edge-judge"
PRODUCER_ACTION_TYPE = "judge"

# A producer-local provisional reason (not a §3.1 ProvisionalReason): an evidence node with no
# EVIDENCED_BY Proposition has no provenance to gate on, so its high-stakes moves are quarantined
# conservatively (V7) — surfaced, never silently driven. A deductive conclusion's DERIVED_FROM
# provenance is a future refinement of this conservative default.
MISSING_PROVENANCE = "missing_provenance"


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
    """

    statement: str
    tier: Tier | None
    box: str | None


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
class QuarantineRecord:
    """One evidential edge dropped because a provisional source would drive a high-stakes move.

    The §3.1 / V7 triage signal: ``reasons`` are the source atom's
    :class:`~iknos.types.epistemic.ProvisionalReason` values (or :data:`MISSING_PROVENANCE`),
    ``stakes`` why the move was high-stakes (a ``REFUTES`` or a sole-support ``SUPPORTS``). Recorded
    on the producing ``Action`` (``outputs.quarantined``) and surfaced on the result — the edge is
    **not** persisted (record-and-skip, never a silent drop); it lifts when the source is confirmed.
    """

    evidence: NodeId
    hypothesis: NodeId
    sign: EdgeSign
    reasons: tuple[str, ...]
    stakes: Stakes

    def to_audit(self) -> dict[str, Any]:
        """The Action-output record (§10.1) — the triage queue's input."""
        return {
            "evidence": self.evidence,
            "sign": str(self.sign),
            "reasons": list(self.reasons),
            "stakes": str(self.stakes),
        }


def edge_stakes(sign: EdgeSign, *, support_count_in_plan: int) -> Stakes:
    """The §3.1 stakes of a would-be edge (V7) — pure.

    ``HIGH`` for any ``REFUTES`` (overturns a hypothesis) and for a ``SUPPORTS`` that is the
    hypothesis's **sole** support in this plan (``support_count_in_plan <= 1`` — a lone supporter
    carries the hypothesis on its own); ``LOW`` for a ``SUPPORTS`` among others (corroboration). The
    count is over the judged ``SUPPORTS`` edges for the hypothesis *before* any quarantine drop.
    """
    if sign is EdgeSign.REFUTES:
        return Stakes.HIGH
    return Stakes.HIGH if support_count_in_plan <= 1 else Stakes.LOW


@dataclass(frozen=True)
class HypothesisPlan:
    """The write plan for one hypothesis: its edges, provenance Action, and quarantine drops."""

    hypothesis: NodeId
    edges: tuple[EdgeWrite, ...]
    action: PlannedAction
    quarantined: tuple[QuarantineRecord, ...] = ()


def evidential_edge_props(
    *,
    box: str | None,
    sign: EdgeSign,
    strength: float,
    significance: float,
    sign_stable: bool,
    now: datetime,
) -> dict[str, Any]:
    """Flatten one evidential edge to AGE properties — the canonical write contract (cf.
    ``derive.derivation_edge_props``).

    Carries the three §8/§9 quantities (``sign``/``strength``/``significance``), the
    ``sign_stable`` finding (so the §7.2 ensemble gate can *graph-query* unstable directional edges
    before authorising a ``refuted`` flip — a first-class signal, not buried only in the Action),
    and bitemporal fields stamped **open** (``valid_to``/``event_time`` null), so a retraction
    stamps ``valid_to`` and the QBAF adapter's current-state filter drops the edge (§10). ``sign``
    is stored redundantly with the label for ``EvidentialEdge``-schema fidelity; the QBAF adapter
    takes direction from the label, the canonical source.
    """
    return {
        "box": box,
        "sign": str(sign),
        "strength": strength,
        "significance": significance,
        "sign_stable": sign_stable,
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
    provisional_reasons: Mapping[NodeId, list[str]],
    policy: SignificancePolicy,
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
    planned with the calibrated ``strength`` (§8) and the structural ``sign`` (the label).

    **The §3.1 quarantine gate (V7).** Before an edge is planned, its :class:`Stakes` is derived
    (:func:`edge_stakes` — ``REFUTES`` or sole-support ``SUPPORTS`` is ``HIGH``) and
    :func:`~iknos.core.quarantine.assert_not_quarantined` is called with the **evidence** node's
    ``provisional_reasons``. A quarantined edge is **dropped from the plan** (not persisted) and
    recorded as a :class:`QuarantineRecord` on the result + the Action's ``outputs.quarantined`` — a
    triage signal, never a silent skip and never an abort (other edges/hypotheses are unaffected).

    The panel tally is folded into the Action provenance (§10.1) — the dropped-``irrelevant`` pairs
    (auditable as *considered and rejected*) and the ``prompt_sha`` / ``schema_sha`` / ``version``
    (so a re-judgment under a changed pipeline is detectable). The edge inherits the **target
    hypothesis's** box.
    """
    hyp_box = node_meta[judgment.hypothesis].box if judgment.hypothesis in node_meta else None
    # Sole-support test (§3.1): count the judged SUPPORTS *before* any quarantine drop, so dropping
    # one provisional supporter does not retroactively promote another to "sole".
    support_count = sum(1 for j in judgment.judgments if j.sign is EdgeSign.SUPPORTS)

    edges: list[EdgeWrite] = []
    edge_provenance: list[dict[str, Any]] = []
    quarantined: list[QuarantineRecord] = []
    for j in judgment.judgments:
        reasons = tuple(provisional_reasons.get(j.evidence, []))
        stakes = edge_stakes(j.sign, support_count_in_plan=support_count)
        try:
            assert_not_quarantined(reasons, stakes)
        except QuarantinedPropositionError:
            # Record-and-skip (§3.1, V7): the provisional source's high-stakes edge is dropped from
            # the plan and surfaced for triage, never persisted.
            quarantined.append(
                QuarantineRecord(
                    evidence=j.evidence,
                    hypothesis=judgment.hypothesis,
                    sign=j.sign,
                    reasons=reasons,
                    stakes=stakes,
                )
            )
            continue
        ev_meta = node_meta.get(j.evidence)
        significance = edge_significance(
            policy,
            ev_meta.tier if ev_meta is not None else None,
            credibility.get(j.evidence),
        )
        props = evidential_edge_props(
            box=hyp_box,
            sign=j.sign,
            strength=j.strength,
            significance=significance,
            sign_stable=j.sign_stable,
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
        edge_provenance.append(_edge_audit(j, significance))

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
            # The §3.1 quarantine drops — a triage signal, not an error (V7).
            "quarantined": [q.to_audit() for q in quarantined],
        },
        model=model,
        sampling=sampling,
    )
    return HypothesisPlan(
        hypothesis=judgment.hypothesis,
        edges=tuple(edges),
        action=action,
        quarantined=tuple(quarantined),
    )


def _edge_audit(j: EdgeJudgment, significance: float) -> dict[str, Any]:
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


@dataclass(frozen=True)
class EdgeProductionResult:
    """The outcome of :meth:`EdgeProducer.produce` (§8, §13).

    ``edges`` are the persisted survivors; ``dropped`` the ``(evidence, hypothesis)`` pairs the
    panel judged ``irrelevant`` (recall→precision handoff, §5.1); ``quarantined`` the
    :class:`QuarantineRecord`s a provisional source would have driven into a high-stakes move (§3.1,
    V7 — dropped from the plan, never persisted, surfaced for triage); ``unstable`` the persisted
    edges whose panel split *direction* (``sign_stable=False``) — the §13 findings the §7.2 ensemble
    gate (G4.5) must clear before a ``refuted`` flip, surfaced not smoothed. ``action_ids`` are the
    provenance Actions written (one per judged hypothesis).
    """

    edges: tuple[ProducedEdge, ...] = ()
    dropped: tuple[tuple[NodeId, NodeId], ...] = ()
    quarantined: tuple[QuarantineRecord, ...] = ()
    action_ids: tuple[uuid.UUID, ...] = ()

    @property
    def unstable(self) -> tuple[ProducedEdge, ...]:
        """The persisted edges with an unstable sign — the gate's input (§7.2, §13)."""
        return tuple(e for e in self.edges if not e.sign_stable)

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
        concurrency: int = 4,
    ) -> None:
        self.judge = judge
        self.candidates = candidates or CandidateGenerationAdapter()
        self.policy = policy
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
                "RETURN n.id, n.statement, n.tier, n.box",
                returns="nid agtype, statement agtype, tier agtype, box agtype",
            )
            for nid, statement, tier, box in rows:
                meta[unquote_agtype(nid)] = NodeMeta(
                    statement=_opt_str(statement) or "",
                    tier=_opt_tier(tier),
                    box=_opt_str(box),
                )
        return meta

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

    async def _load_provisional_reasons(
        self, session: object, evidence_ids: Iterable[NodeId]
    ) -> dict[NodeId, list[str]]:
        """Each evidence node's provisional reasons (§3.1, R8/V7) — the quarantine-gate input.

        A Fact/Conclusion inherits the **union** of ``provisional_reasons`` over the
        ``Proposition``s it is ``EVIDENCED_BY`` (R8). One query over all current evidenced nodes
        (investigation scale, as the other producer reads); ``provisional_reasons`` is a JSON-string
        list, decoded a second time (:func:`_decode_reasons_list`). An evidence node with **no**
        ``EVIDENCED_BY`` ``Proposition`` has no provenance to gate on, so it is treated as
        quarantined with :data:`MISSING_PROVENANCE` (conservative — its high-stakes moves are
        surfaced, never silently driven) and a warning is logged. Total map over ``evidence_ids``.
        """
        from iknos.db.age import execute_cypher, parse_agtype_map, unquote_agtype

        ids = set(evidence_ids)
        acc: dict[NodeId, set[str]] = {}
        rows = await execute_cypher(
            session,  # type: ignore[arg-type]
            "MATCH (n)-[:EVIDENCED_BY]->(p:Proposition) WHERE n.valid_to IS NULL "
            "RETURN n.id, properties(p)",
            returns="nid agtype, props agtype",
        )
        for nid_raw, props_raw in rows:
            nid = unquote_agtype(nid_raw)
            if nid not in ids:
                continue
            reasons = parse_agtype_map(props_raw).get("provisional_reasons")
            acc.setdefault(nid, set()).update(_decode_reasons_list(reasons))

        out: dict[NodeId, list[str]] = {}
        for eid in ids:
            if eid in acc:
                out[eid] = sorted(acc[eid])
            else:
                logger.warning(
                    "evidence node %s has no EVIDENCED_BY Proposition; quarantining its "
                    "high-stakes moves with reason %r (V7)",
                    eid,
                    MISSING_PROVENANCE,
                )
                out[eid] = [MISSING_PROVENANCE]
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
        provisional_reasons = await self._load_provisional_reasons(session, evidence_ids)

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
        quarantined: list[QuarantineRecord] = []
        action_ids: list[uuid.UUID] = []
        for judgment in judgments:
            dropped.extend((ev, judgment.hypothesis) for ev in judgment.irrelevant)
            if not judgment.judgments:
                continue  # nothing survived for this hypothesis — no edges, no Action
            plan = plan_hypothesis(
                judgment,
                node_meta=node_meta,
                credibility=credibility,
                provisional_reasons=provisional_reasons,
                policy=self.policy,
                now=now,
                model=getattr(self.judge.llm, "model", None),
                sampling=self.judge.sampling,
                prompt_sha=self.judge.prompt_sha(),
                schema_sha=self.judge.schema_sha(),
                schema_version=self.judge.SCHEMA_VERSION,
            )
            quarantined.extend(plan.quarantined)
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
            # Build the result rows from the *planned* edges — never the raw judgments — so a
            # quarantine-dropped edge (§3.1, V7) is reported as quarantined, not as persisted.
            for edge in plan.edges:
                produced.append(
                    ProducedEdge(
                        evidence=edge.src_id,
                        hypothesis=edge.dst_id,
                        sign=EdgeSign(edge.props["sign"]),
                        strength=edge.props["strength"],
                        significance=edge.props["significance"],
                        sign_stable=edge.props["sign_stable"],
                    )
                )

        await session.commit()  # type: ignore[attr-defined]
        return EdgeProductionResult(
            edges=tuple(produced),
            dropped=tuple(dropped),
            quarantined=tuple(quarantined),
            action_ids=tuple(action_ids),
        )


def _decode_reasons_list(raw: object) -> list[str]:
    """Decode a ``provisional_reasons`` property (R8) — a JSON-string list — into ``list[str]``.

    ``parse_agtype_map`` returns a list property as its JSON string (``cypher_map`` json-encoded
    it), so it is decoded a second time; ``None``/absent (a pre-R8 evidenced node) → empty
    (non-provisional, *not* missing-provenance — it still has a Proposition). Tolerates an
    already-decoded list.
    """
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        return [str(x) for x in json.loads(raw)]
    return []


def _opt_str(v: object) -> str | None:
    """Parse an agtype scalar that may be SQL/agtype null into ``str | None`` (mirrors the
    derivation/QBAF/candidate adapters' null-tolerant parse)."""
    if v is None or str(v) == "null":
        return None
    from iknos.db.age import unquote_agtype

    return unquote_agtype(v)


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
