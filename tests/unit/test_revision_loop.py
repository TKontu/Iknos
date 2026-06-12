"""Unit tests for the composed-loop orchestrator's pure core (W1; §7.2, §12, §13).

DB-free: :class:`~iknos.core.revision_loop.RevisionPlan` runs the whole loop in memory over
hand-built rows, so the spine logic — Layer A/B → QBAF base wiring, the gate-gated revision, the
``stabilize``-driven fixpoint/oscillation — is pinned without a graph (the live persistence is the
integration test). Gate decisions are built through the **real** ``ensemble_gate.authorise`` (the
spec: don't mock the gate).
"""

import pytest

from iknos.core.composed_loop import Stability, StabilizationResult
from iknos.core.derivation_adapter import DerivedRow, NodeRow
from iknos.core.ensemble_gate import DEFAULT_GATE, GateChannel, affirming, authorise
from iknos.core.qbaf_adapter import EvidenceRow, HypothesisVerdict
from iknos.core.revision_loop import (
    RevisionPlan,
    RevisionResult,
    RevisionSnapshot,
    no_decisions,
    retract_authorised_refuted,
)
from iknos.types.edges import EdgeSign
from iknos.types.intentional import HypothesisState

# An authorising gate decision via the real authorise (DEFAULT_GATE needs {LLM, SYMBOLIC}).
_AUTHORISED = authorise(
    [affirming(GateChannel.LLM), affirming(GateChannel.SYMBOLIC)], gate=DEFAULT_GATE
)


def _authorise_all_refuted(verdicts):  # noqa: ANN001
    """A decider that authorises every structurally-refuted hypothesis (the W2-style injection)."""
    return {v.id: _AUTHORISED for v in verdicts if v.state is HypothesisState.REFUTED}


def _plan(*, decide=no_decisions, revise=retract_authorised_refuted, max_iterations=50, **kw):  # noqa: ANN001
    """A minimal one-hypothesis plan: fact ``f`` REFUTES hypothesis ``h`` (net attack → REFUTED)."""
    base = dict(
        nodes=(NodeRow("h", "b1", 0.5), NodeRow("f", "b1", 1.0)),
        base_fact_ids=frozenset({"f"}),
        derived=(),
        edges=(EvidenceRow(source="f", target="h", sign=EdgeSign.REFUTES, strength=0.9),),
        box_ids=frozenset({"b1"}),
        hypothesis_ids=frozenset({"h"}),
    )
    base.update(kw)
    return RevisionPlan(decide=decide, revise=revise, max_iterations=max_iterations, **base)


# --- the pure policies -------------------------------------------------------------------------


def test_no_decisions_authorises_nothing() -> None:
    assert no_decisions(()) == {}


def test_default_revise_retracts_only_authorised_refutations() -> None:
    refuted = HypothesisVerdict(
        id="h", acceptability=0.05, band=_band(0.05), state=HypothesisState.REFUTED
    )
    # Authorised → retracted; no decision → left in place (held by V8, not retracted).
    assert retract_authorised_refuted([refuted], {"h": _AUTHORISED}, frozenset()) == frozenset(
        {"h"}
    )
    assert retract_authorised_refuted([refuted], {}, frozenset()) == frozenset()
    # A non-refuted verdict is never retracted, even with a decision present.
    supported = HypothesisVerdict("h", 0.9, _band(0.9), HypothesisState.SUPPORTED)
    assert retract_authorised_refuted([supported], {"h": _AUTHORISED}, frozenset()) == frozenset()


# --- the loop (pure, in-memory) ----------------------------------------------------------------


def test_no_authorisation_converges_in_one_pass_without_retracting() -> None:
    # The safe default: the REFUTES is computed but not authorised → nothing retracted, converges.
    stab, final = _plan(decide=no_decisions).run()
    assert stab.status is Stability.CONVERGED
    assert final.retracted == frozenset()
    (v,) = final.verdicts
    assert v.id == "h" and v.state is HypothesisState.REFUTED  # the structural finding stands
    assert final.decisions == {}  # held by V8 at persist time, not flipped here


def test_authorised_refutation_retracts_the_hypothesis_then_converges() -> None:
    stab, final = _plan(decide=_authorise_all_refuted).run()
    assert stab.status is Stability.CONVERGED
    # h was authorised-refuted → retracted; the next pass (h gone) is a fixpoint.
    assert final.retracted == frozenset({"h"})
    assert stab.iterations == 2
    assert stab.trajectory == (frozenset(), frozenset({"h"}))
    assert final.verdicts == ()  # h excluded from adjudication once retracted


def test_layer_a_propagates_a_retracted_base_fact_through_the_qbaf_base() -> None:
    # f --DERIVED_FROM--> c (so c is grounded only via f); c --SUPPORTS--> h. Retracting f should
    # drop c's well-founded support → c's QBAF base falls to 0 → h loses its support.
    plan = _plan(
        nodes=(
            NodeRow("h", "b1", 0.3),
            NodeRow("f", "b1", 1.0),
            NodeRow("c", "b1", 1.0),
        ),
        base_fact_ids=frozenset({"f"}),
        derived=(DerivedRow(conclusion="c", antecedent="f", derivation="d1", strength=1.0),),
        edges=(EvidenceRow(source="c", target="h", sign=EdgeSign.SUPPORTS, strength=0.9),),
        hypothesis_ids=frozenset({"h"}),
    )
    with_f = plan.adjudicate_at(frozenset())
    (v0,) = with_f.verdicts
    assert v0.state is HypothesisState.SUPPORTED  # c grounds h
    assert with_f.confidence["c"] == pytest.approx(1.0)

    without_f = plan.adjudicate_at(frozenset({"f"}))
    (v1,) = without_f.verdicts
    assert v1.state is not HypothesisState.SUPPORTED  # c lost grounding → h unsupported
    assert v1.acceptability < v0.acceptability
    assert "c" not in without_f.confidence  # c is no longer well-founded-supported


def test_oscillating_revision_policy_is_surfaced_as_a_finding() -> None:
    # A toy revision policy that toggles the retracted set forever — the stabilize driver must
    # surface it as a finding (oscillation), never loop on it (§13).
    def toggling(_verdicts, _decisions, retracted):  # noqa: ANN001
        return frozenset() if "h" in retracted else frozenset({"h"})

    stab, _final = _plan(revise=toggling, max_iterations=20).run()
    assert stab.status is Stability.OSCILLATING
    assert stab.is_finding
    assert stab.cycle  # the unstable sub-region to surface (§12/§13)
    assert stab.unstable_region() == stab.cycle


def test_diverging_policy_hits_the_bound_and_is_a_finding() -> None:
    # A policy that always grows the retracted set with a fresh sentinel never repeats or fixes —
    # it must hit the bound (DIVERGED), not loop unboundedly.
    def ever_growing(_verdicts, _decisions, retracted):  # noqa: ANN001
        return retracted | {f"x{len(retracted)}"}

    stab, _final = _plan(revise=ever_growing, max_iterations=5).run()
    assert stab.status is Stability.DIVERGED
    assert stab.iterations == 5
    assert stab.is_finding


# --- result surface ----------------------------------------------------------------------------


def test_revision_result_is_finding_tracks_both_loop_and_held() -> None:
    converged = StabilizationResult(
        status=Stability.CONVERGED, state=frozenset(), iterations=1, trajectory=(frozenset(),)
    )
    snap = RevisionSnapshot(frozenset(), (), {}, {}, qbaf_converged=True, qbaf_unstable=frozenset())
    # Converged + nothing persisted/held → not a finding.
    assert RevisionResult(stabilization=converged, final=snap).is_finding is False
    # An oscillating loop is a finding regardless of persistence.
    osc = StabilizationResult(
        status=Stability.OSCILLATING,
        state=frozenset({"h"}),
        iterations=3,
        trajectory=(frozenset(), frozenset({"h"})),
        cycle=(frozenset({"h"}), frozenset()),
    )
    assert RevisionResult(stabilization=osc, final=snap).is_finding is True


def _band(acc: float):  # noqa: ANN202
    from iknos.types.intentional import band

    return band(acc)
