"""G4.1 — the QBAF engine (:func:`solve`) and the verdict/state read-off (§7.2, §11.2, §13).

Mirrors ``test_confidence_valuation.py``: exact behaviour on acyclic frameworks, a bounded
convergent fixpoint on cyclic ones, and — the §13 headline — **non-convergence surfaced as a
finding, never smoothed into a verdict**. Plus the read-off: acceptability → §11.2 verdict band
and the computed hypothesis state (with the ensemble-gated ``refuted`` flip the documented
deferred seam).
"""

import pytest

from iknos.core.qbaf import (
    BAF,
    Edge,
    aggregate_evidence,
    classify_state,
    solve,
)
from iknos.types.intentional import HypothesisState

# --------------------------------------------------------------------------------------------
# The engine: acyclic exactness, base-only, cyclic convergence, non-convergence surfacing
# --------------------------------------------------------------------------------------------


def test_base_only_framework_returns_base_in_one_sweep() -> None:
    """No edges ⇒ acceptability is exactly the base scores, settled immediately."""
    out = solve(BAF(frozenset({"a", "b", "c"})), base={"a": 0.9, "b": 0.5, "c": 0.1})
    assert out.acceptability == pytest.approx({"a": 0.9, "b": 0.5, "c": 0.1})
    assert out.converged and out.iterations == 1


def test_acyclic_chain_converges_to_the_hand_computed_fixpoint() -> None:
    """A support chain ``s → m → h`` (DF-QuAD): each step propagates ``edge·σ(src)``. Exact,
    deterministic, converged."""
    baf = BAF(
        arguments=frozenset({"s", "m", "h"}),
        supports=(Edge("s", "m", 0.8), Edge("m", "h", 0.5)),
    )
    out = solve(baf, base={"s": 1.0})  # m, h default to base 0.0
    # σ(s)=1.0 (leaf); σ(m)=combine(0, 0.8·1.0, 0)=0.8; σ(h)=combine(0, 0.5·0.8, 0)=0.4.
    assert out.acceptability == pytest.approx({"s": 1.0, "m": 0.8, "h": 0.4})
    assert out.converged


def test_cyclic_mutual_support_converges_to_a_bounded_fixpoint_no_inflation() -> None:
    """Mutual support ``a ⇄ b`` with sub-unit edges converges to a saturated fixpoint strictly
    below 1.0 — the cyclic case settles, it does not inflate (DF-QuAD)."""
    baf = BAF(
        arguments=frozenset({"a", "b"}),
        supports=(Edge("a", "b", 0.5), Edge("b", "a", 0.5)),
    )
    out = solve(baf, base={"a": 0.5, "b": 0.5})
    # Fixpoint: σ = 0.5 + 0.5·(0.5·σ) ⇒ σ = 0.5 / 0.75 = 2/3.
    assert out.converged
    assert out.acceptability["a"] == pytest.approx(2 / 3)
    assert out.acceptability["b"] == pytest.approx(2 / 3)
    assert out.acceptability["a"] < 1.0


def test_nonconvergence_is_surfaced_as_a_finding_not_smoothed() -> None:
    """When the iteration bound is hit without settling, the still-moving region is returned in
    ``unstable`` and ``is_finding`` is true — §13: surface the unresolved region, never silently
    re-iterate or report a false verdict.

    Mutual support with strength-1.0 edges converges to 1.0 only geometrically; a tiny bound
    deterministically stops it short, exercising the surfacing contract. (Period-true
    oscillation over *discrete* states is the outer ``composed_loop.stabilize`` driver's job;
    here the contract is "bound hit ⇒ unresolved region surfaced".)
    """
    baf = BAF(
        arguments=frozenset({"a", "b"}),
        supports=(Edge("a", "b", 1.0), Edge("b", "a", 1.0)),
    )
    out = solve(baf, base={"a": 0.5, "b": 0.5}, max_iterations=3)
    assert not out.converged
    assert out.is_finding
    assert out.unstable == frozenset({"a", "b"})
    assert out.iterations == 3


def test_same_cycle_converges_when_given_enough_iterations() -> None:
    """The same framework *does* converge (to 1.0) under a generous bound — confirming the
    finding above was the bound, not a defect."""
    baf = BAF(
        arguments=frozenset({"a", "b"}),
        supports=(Edge("a", "b", 1.0), Edge("b", "a", 1.0)),
    )
    out = solve(baf, base={"a": 0.5, "b": 0.5}, max_iterations=200)
    assert out.converged
    assert out.acceptability["a"] == pytest.approx(1.0)


def test_determinism_same_inputs_same_output() -> None:
    baf = BAF(
        arguments=frozenset({"h", "p", "q"}),
        supports=(Edge("p", "h", 0.6),),
        attacks=(Edge("q", "h", 0.4),),
    )
    base = {"h": 0.5, "p": 0.9, "q": 0.7}
    assert solve(baf, base=base).acceptability == solve(baf, base=base).acceptability


def test_rejects_bad_bound_and_out_of_range_scores() -> None:
    baf = BAF(frozenset({"a"}))
    with pytest.raises(ValueError, match="max_iterations"):
        solve(baf, base={"a": 0.5}, max_iterations=0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        solve(baf, base={"a": 1.5})
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        solve(BAF(frozenset({"a", "b"}), supports=(Edge("a", "b", 2.0),)), base={"a": 0.5})


# --------------------------------------------------------------------------------------------
# The Layer B seam: base score = Layer B confidence; acceptability is COMPUTED, not hand-set
# --------------------------------------------------------------------------------------------


def test_acceptability_is_computed_from_evidence_not_the_base_score() -> None:
    """The hypothesis's base score is its Layer B confidence (§12 seam); incoming evidence then
    moves acceptability away from it — the engine computes the verdict, it is never the raw base
    or a hand-set value (§10 "engine disposes")."""
    base = {"h": 0.5, "p": 0.9, "q": 0.9}
    supported = solve(
        BAF(frozenset({"h", "p"}), supports=(Edge("p", "h", 0.8),)), base=base
    ).acceptability["h"]
    attacked = solve(
        BAF(frozenset({"h", "q"}), attacks=(Edge("q", "h", 0.8),)), base=base
    ).acceptability["h"]
    assert supported > 0.5  # support lifts it above the base
    assert attacked < 0.5  # attack pulls it below the base


# --------------------------------------------------------------------------------------------
# Read-off: aggregate evidence + computed hypothesis state (banding itself lives in
# types/intentional.py and is tested in test_intentional.py — not duplicated here).
# --------------------------------------------------------------------------------------------


def test_aggregate_evidence_recovers_per_node_support_and_attack() -> None:
    """``aggregate_evidence`` returns the same per-node contributions ``solve`` folds, so
    ``classify_state`` can be fed the support/attack at a hypothesis (DF-QuAD prob-sum)."""
    baf = BAF(
        arguments=frozenset({"h", "p1", "p2", "q"}),
        supports=(Edge("p1", "h", 1.0), Edge("p2", "h", 1.0)),
        attacks=(Edge("q", "h", 1.0),),
    )
    out = solve(baf, base={"h": 0.3, "p1": 0.5, "p2": 0.5, "q": 0.8})
    supp, att = aggregate_evidence(baf, out.acceptability)["h"]
    # supporters contribute 1.0·0.5 each → prob-sum(0.5, 0.5) = 0.75; attacker 1.0·0.8 = 0.8.
    assert supp == pytest.approx(0.75)
    assert att == pytest.approx(0.8)


def test_classify_state_supported_refuted_unsupported() -> None:
    """State is computed (§10), distinguishing actively-refuted from merely-unsupported. The
    support bar is the §11.2 ``plausible`` boundary (single source of truth in intentional.py)."""
    # Bands to plausible/true (≥ 0.5) ⇒ supported.
    assert (
        classify_state(acceptability=0.8, aggregate_support=0.9, aggregate_attack=0.1)
        is HypothesisState.SUPPORTED
    )
    # Below the bar AND net attack dominates ⇒ refuted (actively out-argued).
    assert (
        classify_state(acceptability=0.2, aggregate_support=0.1, aggregate_attack=0.7)
        is HypothesisState.REFUTED
    )
    # Below the bar with no net attack ⇒ unsupported (just insufficient support).
    assert (
        classify_state(acceptability=0.3, aggregate_support=0.2, aggregate_attack=0.0)
        is HypothesisState.UNSUPPORTED
    )
