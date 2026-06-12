"""The composed-loop orchestrator — the reasoning core's outer feedback spine (W1; §7.2, §12, §13).

The per-layer cores are each verified, but until now **nothing owned the cross-layer control flow**:
``composed_loop.stabilize`` (G3.9) was implemented and never called, so a retraction never triggered
re-adjudication — a change was only picked up on the next independent read. This module is the one
owner of the §12 feedback loop ``retract → Layer A → Layer B → QBAF → ensemble gate → persist``, so
belief revision and the V8 consumer-filter share a single sequencing instead of divergent ad-hoc
wirings.

**Pure loop, DB at the edges.** ``stabilize`` is a *pure, synchronous* driver (``step: S → S``); the
graph work is async. The fit (and the codebase's pure/DB discipline) is: **load the active subgraph
once**, run the loop **in memory** over the *retracted-node set* via the pure cores
(:func:`~iknos.core.derivation_adapter.support_and_confidence` for Layer A/B,
:func:`~iknos.core.qbaf_adapter.assemble_baf`/:func:`~iknos.core.qbaf_adapter.adjudicate` for the
QBAF, :func:`~iknos.core.ensemble_gate.authorise` via the injected decider for the gate), then
**persist once** at the fixpoint. So ``stabilize`` is the *only* loop driver — there is no ad-hoc
retry loop around the adapters — and the iteration touches no DB.

**The step.** The loop state ``S`` is the **frozenset of retracted node ids** (it determines
everything else deterministically). One ``step(retracted)``:

1. re-assemble the active subgraph with ``retracted`` excluded → Layer A certifies well-founded
   support, Layer B scores it; a derivation-governed node that lost support reads as confidence
   ``0`` (revivable), a non-derivation node (a ``Hypothesis``) keeps its seed — the QBAF base score;
2. assemble + adjudicate the QBAF over the surviving nodes/edges → per-``Hypothesis`` verdicts;
3. ``decide`` the gate decisions for the structurally-refuted hypotheses (injected — the LLM/
   symbolic/temporal channels live outside; W2 feeds pre-built decisions through the real
   ``authorise``);
4. ``revise`` returns the next retracted set — **default**: retract each *authorised*-refuted
   hypothesis (its outgoing ``SUPPORTS``/``REFUTES`` vanish → its dependents re-ground next pass —
   the §12 ``REFUTES → retract`` feedback). A domain/fixture may inject a richer policy (e.g.
   retract the contradicted supporting fact).

``stabilize`` runs this to a fixpoint under a hard bound; **non-convergence is a finding** (§13):
an oscillating or diverged loop is surfaced with its unstable region (the retracted-set cycle and
the hypotheses still in play), never silently re-iterated *or* smoothed into a verdict. Every
iteration appends an ``Action`` (§10.1, the audit trail).

**Persistence — only at a genuine fixpoint.** On a ``CONVERGED``
:class:`~iknos.core.composed_loop.Stability` the loop persists: ``persist_verdicts`` (the V8 gate
filter, on the surviving hypotheses) → retract
the converged retracted set (``valid_to``) → write back the recomputed Layer B ``confidence`` so a
later independent ``evaluate`` stays consistent. On a non-converged outcome it persists **nothing**
(no false commitment) — only the finding + the per-iteration Actions.

**Thin (the W1 scope).** No value-of-information, no re-inference budget (Phase 5/6 layers), a
single working box, invoked explicitly — no daemon. The symbolic/temporal channel *producers* are
not built
here (W3 / later G4.5); this consumes their (injected) decisions.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from iknos.core.composed_loop import StabilizationResult, stabilize
from iknos.core.confidence import Confidence
from iknos.core.derivation_adapter import (
    DerivedRow,
    NodeRow,
    assemble_subgraph,
    load_active_box_ids,
    load_base_fact_ids,
    load_derived_rows,
    load_hypothesis_ids,
    load_reasoning_nodes,
    support_and_confidence,
)
from iknos.core.ensemble_gate import GateDecision
from iknos.core.qbaf_adapter import (
    EvidenceRow,
    HypothesisVerdict,
    PersistResult,
    QbafAdapter,
    adjudicate,
    assemble_baf,
    load_evidential_edges,
)
from iknos.core.truth_maintenance import NodeId
from iknos.types.intentional import HypothesisState

# The actor/action_type stamped on every per-iteration Action (§10.1) — the audit handle a reader
# greps for to reconstruct "what did the revision loop do, iteration by iteration".
LOOP_ACTOR = "revision-loop"
LOOP_ACTION_TYPE = "stabilize-step"

#: A default iteration bound — generous enough for real belief revision to settle, finite so an
#: oscillating/diverged region is *surfaced* (§13) rather than looped on. The caller may override.
DEFAULT_MAX_ITERATIONS = 50

# Given the verdicts of one adjudication, the gate decision per (structurally-refuted) hypothesis.
# Injected: the LLM/symbolic/temporal channels live outside this module (W2 feeds decisions built
# through the real ``ensemble_gate.authorise``); the loop only consumes them.
GateDecider = Callable[[Sequence[HypothesisVerdict]], Mapping[NodeId, GateDecision]]

# Given the verdicts, the gate decisions, and the current retracted set, the **next** retracted set
# — the §12 ``REFUTES → retract`` belief-revision policy. Returning the same set is a fixpoint.
Reviser = Callable[
    [Sequence[HypothesisVerdict], Mapping[NodeId, GateDecision], frozenset[NodeId]],
    frozenset[NodeId],
]


def no_decisions(_verdicts: Sequence[HypothesisVerdict]) -> Mapping[NodeId, GateDecision]:
    """The safe-by-default decider: **no** authorising decisions (§7.2, principle 6).

    With no gate decision every structural refutation is *held* (V8) and nothing is retracted, so
    the loop converges in one pass leaving the graph unflipped — the conservative default until a
    caller
    injects real channel decisions. Mirrors ``DEFAULT_GATE`` withholding while the producers are
    unwired."""
    return {}


def retract_authorised_refuted(
    verdicts: Sequence[HypothesisVerdict],
    decisions: Mapping[NodeId, GateDecision],
    retracted: frozenset[NodeId],
) -> frozenset[NodeId]:
    """Default revision policy: retract each **authorised**-refuted hypothesis (§12, W1 default).

    A hypothesis the QBAF computed ``refuted`` *and* the gate **authorised** is retracted — its
    outgoing ``SUPPORTS``/``REFUTES`` edges vanish, so whatever it fed re-grounds on the next pass
    (the ``REFUTES → retract`` feedback). Accumulates onto ``retracted`` (monotone → always
    converges); a domain policy that revives nodes can be injected instead, and *that* is what can
    oscillate (surfaced as a finding). A computed ``refuted`` the gate did **not** authorise is left
    in place — held by ``persist_verdicts`` (V8), never retracted on an un-authorised flip.
    """
    newly = {
        v.id
        for v in verdicts
        if v.state is HypothesisState.REFUTED and v.id in decisions and decisions[v.id].authorised
    }
    return retracted | newly


@dataclass(frozen=True)
class RevisionSnapshot:
    """One adjudication of the graph at a given retracted set — the pure step's full read-off.

    ``verdicts`` are the per-``Hypothesis`` verdicts over the surviving nodes; ``decisions`` the
    gate's verdict per refuted hypothesis; ``confidence`` the recomputed Layer B value per supported
    node (the QBAF base score, written back on convergence); ``qbaf_converged``/``qbaf_unstable``
    *inner* QBAF convergence (an evidential-cycle finding, §13, distinct from the *outer* loop's).
    """

    retracted: frozenset[NodeId]
    verdicts: tuple[HypothesisVerdict, ...]
    decisions: dict[NodeId, GateDecision]
    confidence: dict[NodeId, Confidence]
    qbaf_converged: bool
    qbaf_unstable: frozenset[NodeId]


@dataclass(frozen=True)
class RevisionPlan:
    """The active subgraph loaded once + the injected policies — the pure, in-memory loop (W1).

    Holds the raw rows (so each step re-assembles with a different retracted set, the pure-core
    discipline) and the ``decide``/``revise`` policies. :meth:`run` drives ``stabilize`` over the
    retracted-node set with no DB access — the :class:`RevisionLoop` does the surrounding I/O.
    """

    nodes: tuple[NodeRow, ...]
    base_fact_ids: frozenset[NodeId]
    derived: tuple[DerivedRow, ...]
    edges: tuple[EvidenceRow, ...]
    box_ids: frozenset[str]
    hypothesis_ids: frozenset[NodeId]
    decide: GateDecider = no_decisions
    revise: Reviser = retract_authorised_refuted
    max_iterations: int = DEFAULT_MAX_ITERATIONS

    def _derivation_governed(self) -> frozenset[NodeId]:
        """Nodes whose confidence Layer A/B owns (base facts + any ``DERIVED_FROM`` conclusion).

        A node here that is *not* well-founded-supported reads as confidence ``0`` (it lost its
        grounding); a node *not* here (a ``Hypothesis`` with no derivation) keeps its stored seed as
        the QBAF base score (the QBAF, not propagation, adjusts it)."""
        return self.base_fact_ids | frozenset(d.conclusion for d in self.derived)

    def adjudicate_at(self, retracted: frozenset[NodeId]) -> RevisionSnapshot:
        """Re-run Layer A/B → QBAF → gate at ``retracted`` — pure, the heart of one ``step``."""
        active = [n for n in self.nodes if n.id not in retracted]
        active_base = self.base_fact_ids - retracted
        subgraph = assemble_subgraph(active, active_base, self.derived, active_box_ids=self.box_ids)
        _supported, confidence = support_and_confidence(subgraph)

        governed = self._derivation_governed()

        def base_score(n: NodeRow) -> Confidence:
            if n.id in confidence:  # well-founded-supported → its Layer B value
                return confidence[n.id]
            if n.id in governed:  # derivation-governed but unsupported → lost grounding
                return 0.0
            return n.confidence  # not derivation-governed (a Hypothesis) → its stored seed

        qbaf_nodes = [NodeRow(id=n.id, box=n.box, confidence=base_score(n)) for n in active]
        inp = assemble_baf(qbaf_nodes, self.edges, active_box_ids=self.box_ids)
        active_hyps = [h for h in self.hypothesis_ids if h not in retracted]
        result = adjudicate(inp, active_hyps)
        decisions = dict(self.decide(result.verdicts))
        return RevisionSnapshot(
            retracted=retracted,
            verdicts=result.verdicts,
            decisions=decisions,
            confidence=dict(confidence),
            qbaf_converged=result.converged,
            qbaf_unstable=result.unstable,
        )

    def _step(self, retracted: frozenset[NodeId]) -> frozenset[NodeId]:
        snap = self.adjudicate_at(retracted)
        return self.revise(snap.verdicts, snap.decisions, retracted)

    def run(self) -> tuple[StabilizationResult[frozenset[NodeId]], RevisionSnapshot]:
        """Drive ``stabilize`` from the empty retracted set to a fixpoint (pure, no DB).

        Returns the stabilization outcome (converged / oscillating / diverged, with its trajectory +
        unstable region) and the snapshot **at the final state** — the verdicts/decisions/confidence
        the caller persists on convergence (and ignores on a finding)."""
        initial: frozenset[NodeId] = frozenset()
        result = stabilize(initial, self._step, max_iterations=self.max_iterations)
        final = self.adjudicate_at(result.state)
        return result, final


@dataclass(frozen=True)
class RevisionResult:
    """The outcome of :meth:`RevisionLoop.run` (§7.2, §12, §13).

    ``stabilization`` is the loop's termination (its ``is_finding``/``unstable_region`` are the §13
    surface); ``final`` the adjudication at the final state; ``persisted`` the ``persist_verdicts``
    result (``None`` when the loop did not converge — nothing was committed); ``retracted`` the
    nodes retracted on convergence; ``action_ids`` the per-iteration Actions.
    """

    stabilization: StabilizationResult[frozenset[NodeId]]
    final: RevisionSnapshot
    persisted: PersistResult | None = None
    retracted: frozenset[NodeId] = field(default_factory=frozenset)
    action_ids: tuple[uuid.UUID, ...] = ()

    @property
    def converged(self) -> bool:
        """Whether the loop reached a genuine fixpoint and committed (§12)."""
        return self.stabilization.converged

    @property
    def is_finding(self) -> bool:
        """Whether there is an unresolved region to surface (§13): an unstable outer loop, or a
        held refutation persisted with ``pending_refutation`` (V8)."""
        return self.stabilization.is_finding or bool(self.persisted and self.persisted.is_finding)


class RevisionLoop:
    """Loads the active subgraph, runs the pure composed loop, and persists the fixpoint (W1).

    DB-free to construct; the reads/writes happen in :meth:`run`. Stateless across calls — a full
    load -> pure stabilize -> persist per call (the adapter discipline). ``stabilize`` is the sole
    driver; this class is the surrounding I/O.
    """

    def __init__(self, qbaf: QbafAdapter | None = None) -> None:
        self.qbaf = qbaf or QbafAdapter()

    async def run(
        self,
        session: object,
        *,
        decide: GateDecider = no_decisions,
        revise: Reviser = retract_authorised_refuted,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        persist: bool = True,
    ) -> RevisionResult:
        """Run the §12 feedback loop to a fixpoint and persist it (one transaction).

        Loads the active subgraph (the shared module reads, so it cannot diverge from the adapters),
        builds a :class:`RevisionPlan`, drives ``stabilize`` **purely**, records one ``Action`` per
        iteration, and — **only on convergence** — persists the gated verdicts, the retractions, and
        the recomputed confidence. A non-converged loop commits nothing and is a finding.
        """
        box_ids = await load_active_box_ids(session)
        nodes = await load_reasoning_nodes(session)
        base_fact_ids = await load_base_fact_ids(session)
        derived = await load_derived_rows(session)
        edges = await load_evidential_edges(session)
        hypothesis_ids = await load_hypothesis_ids(session)

        plan = RevisionPlan(
            nodes=tuple(nodes),
            base_fact_ids=frozenset(base_fact_ids),
            derived=tuple(derived),
            edges=tuple(edges),
            box_ids=box_ids,
            hypothesis_ids=frozenset(hypothesis_ids),
            decide=decide,
            revise=revise,
            max_iterations=max_iterations,
        )
        stab, final = plan.run()  # pure — no DB inside the loop

        action_ids = await self._record_actions(session, stab, plan)

        persisted: PersistResult | None = None
        retracted: frozenset[NodeId] = frozenset()
        if stab.converged and persist:
            persisted = await self._persist(session, final, plan)
            retracted = final.retracted

        await session.commit()  # type: ignore[attr-defined]
        return RevisionResult(
            stabilization=stab,
            final=final,
            persisted=persisted,
            retracted=retracted,
            action_ids=tuple(action_ids),
        )

    async def _record_actions(
        self,
        session: object,
        stab: StabilizationResult[frozenset[NodeId]],
        plan: RevisionPlan,
    ) -> list[uuid.UUID]:
        """One ``Action`` per iteration (§10.1) — the loop's audit trail, including the finding.

        Walks the ``stabilize`` trajectory (``initial`` first) so each iteration's retracted set is
        recorded; the terminal Action carries the termination status and the unstable region if
        loop did not converge (§13). Records nothing for a zero-iteration run beyond the terminal.
        """
        from iknos.provenance.action_log import record_action

        action_ids: list[uuid.UUID] = []
        trajectory = stab.trajectory
        for i, state in enumerate(trajectory):
            terminal = i == len(trajectory) - 1
            outputs: dict[str, object] = {
                "iteration": i,
                "retracted": sorted(state),
            }
            if terminal:
                outputs["status"] = str(stab.status)
                outputs["converged"] = stab.converged
                if stab.is_finding:
                    # The §13 unstable region: the retracted-set cycle/trajectory, surfaced not
                    # smoothed. The hypotheses still in play are the investigator handle.
                    outputs["unstable_region"] = [sorted(s) for s in stab.unstable_region()]
            action_ids.append(
                await record_action(
                    session,  # type: ignore[arg-type]
                    actor=LOOP_ACTOR,
                    action_type=LOOP_ACTION_TYPE,
                    inputs={"iteration": i, "max_iterations": plan.max_iterations},
                    outputs=outputs,
                    model=None,
                    sampling={},
                )
            )
        return action_ids

    async def _persist(
        self, session: object, final: RevisionSnapshot, plan: RevisionPlan
    ) -> PersistResult:
        """Commit the converged fixpoint: gated verdicts → retractions → confidence write-back.

        Order matters only between the verdict write and the retraction: ``persist_verdicts`` is
        the surviving (non-retracted) hypotheses first (the retracted ones are already excluded from
        ``final.verdicts``), then the retractions stamp ``valid_to``. The recomputed Layer B
        confidence is written back so a later independent ``evaluate`` reads the loop result, not
        stale seed.
        """
        from iknos.db.age import execute_cypher

        persisted = await self.qbaf.persist_verdicts(
            session, final.verdicts, gate_decisions=final.decisions
        )

        for nid in sorted(final.retracted):
            await execute_cypher(
                session,  # type: ignore[arg-type]
                f"MATCH (n {{id: '{nid}'}}) WHERE n.valid_to IS NULL "
                f"SET n.valid_to = '{_now_iso()}'",
            )

        # Write back the recomputed Layer B confidence for every active derivation-governed node, so
        # the stored confidence (the QBAF base score) matches what the loop adjudicated on — a node
        # that *lost* its grounding is absent from final.confidence and is written **0**, not left
        # stale at its old high value (the bug a "write only the supported set" loop would have).
        governed = plan._derivation_governed()
        active_governed = {n.id for n in plan.nodes if n.id not in final.retracted} & governed
        for nid in sorted(active_governed):
            conf = final.confidence.get(nid, 0.0)
            await execute_cypher(
                session,  # type: ignore[arg-type]
                f"MATCH (n {{id: '{nid}'}}) WHERE n.valid_to IS NULL "
                f"SET n.confidence = {float(conf)}",
            )
        return persisted


def _now_iso() -> str:
    """The retraction ``valid_to`` timestamp. A module seam (like the producers stamp ``now``) so a
    test can monkeypatch it; the real clock is read at call time."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
