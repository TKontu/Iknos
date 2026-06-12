"""Unit tests for the SYMBOLIC channel producer — the clingo consistency check (W3, §8(d)).

DB-free, LLM-free: the pure :class:`~iknos.core.symbolic_gate.SymbolicQuery` is hand-built and run
through real clingo (the engine is not mocked — that is the point of W3). Two layers are pinned:
the consistency verdicts (AFFIRM / DISSENT / ABSTAIN, including the transitive-via-rules case) and —
critically for W3 — **which gate is in force**: that wiring the real symbolic channel into
``DEFAULT_GATE`` now *unblocks* an automated ``refuted`` flip (LLM + SYMBOLIC affirm → AUTHORISED),
while a symbolic dissent vetoes and a symbolic abstain still withholds. No test asserted this before
W3; the choice is no longer implicit in ``DEFAULT_GATE``'s definition.
"""

from iknos.core.edge_judge import EdgeJudgment
from iknos.core.ensemble_gate import (
    DEFAULT_GATE,
    ChannelStance,
    GateChannel,
    GateOutcome,
    authorise_from_panel,
)
from iknos.core.subjective_logic import Opinion
from iknos.core.symbolic_gate import (
    Atom,
    Consistency,
    Rule,
    SymbolicQuery,
    check_consistency,
    symbolic_channel_for,
)
from iknos.types.edges import EdgeSign


def _q(hypothesis, refuter, *, context=(), rules=()):  # noqa: ANN001, ANN202
    return SymbolicQuery(
        hypothesis=tuple(hypothesis), refuter=tuple(refuter), context=tuple(context), rules=rules
    )


# --- consistency verdicts ----------------------------------------------------------------------


def test_direct_polarity_contradiction_is_contradictory() -> None:
    # hypothesis asserts claimA; refuter asserts ¬claimA — a literal P ∧ ¬P.
    q = _q((Atom("claimA", True),), (Atom("claimA", False),))
    assert check_consistency(q).verdict is Consistency.CONTRADICTORY


def test_related_same_polarity_is_consistent() -> None:
    # The "refuter" asserts the SAME claim with the SAME polarity — it agrees, it does not refute.
    q = _q((Atom("claimA", True),), (Atom("claimA", True),))
    assert check_consistency(q).verdict is Consistency.CONSISTENT


def test_unrelated_refuter_abstains() -> None:
    # The refuter touches no claim the sub-region mentions — the symbolic layer cannot decide.
    q = _q((Atom("claimA", True),), (Atom("claimZ", False),))
    assert check_consistency(q).verdict is Consistency.UNRELATED


def test_transitive_contradiction_through_a_rule_is_contradictory() -> None:
    # context: b holds; rule: claimA ← b; hypothesis asserts claimA. The refuter negates b, which
    # (through the rule) makes claimA both derivable-true and asserted-false → UNSAT. Only a solver
    # that closes the rule sees this — the reason clingo earns its place over a two-atom set test.
    q = _q(
        (Atom("claimA", True),),
        (Atom("b", False),),
        context=(Atom("b", True),),
        rules=(Rule(Atom("claimA", True), (Atom("b", True),)),),
    )
    assert check_consistency(q).verdict is Consistency.CONTRADICTORY


def test_rule_coupled_but_consistent_refuter_dissents() -> None:
    # The refuter is coupled to the sub-region (shares atom b) but introduces no contradiction →
    # related + SAT → the refutation is not borne out → DISSENT.
    q = _q(
        (Atom("claimA", True),),
        (Atom("b", True),),
        context=(Atom("b", True),),
        rules=(Rule(Atom("claimA", True), (Atom("b", True),)),),
    )
    assert check_consistency(q).verdict is Consistency.CONSISTENT


def test_already_inconsistent_subregion_is_indeterminate() -> None:
    # The hypothesis ∪ context is contradictory on its own (claimA asserted AND negated): the
    # refuter cannot be blamed for it → INDETERMINATE (abstain), never a spurious affirmation.
    q = _q(
        (Atom("claimA", True),),
        (Atom("claimA", False),),
        context=(Atom("claimA", False),),
    )
    assert check_consistency(q).verdict is Consistency.INDETERMINATE


# --- verdict → channel stance ------------------------------------------------------------------


def test_channel_stance_maps_each_verdict() -> None:
    contradictory = symbolic_channel_for(_q((Atom("a", True),), (Atom("a", False),)))
    consistent = symbolic_channel_for(_q((Atom("a", True),), (Atom("a", True),)))
    unrelated = symbolic_channel_for(_q((Atom("a", True),), (Atom("z", False),)))
    for sig in (contradictory, consistent, unrelated):
        assert sig.channel is GateChannel.SYMBOLIC
        assert sig.detail  # every stance carries an audit reason (§10.1)
    assert contradictory.stance is ChannelStance.AFFIRM
    assert consistent.stance is ChannelStance.DISSENT
    assert unrelated.stance is ChannelStance.ABSTAIN


# --- the gate is unblocked: which gate is in force (the W3 pin) ---------------------------------


def _stable_refuter(hyp: str) -> EdgeJudgment:
    """A blind-panel judgment that produced a stable REFUTES edge bearing on ``hyp`` (the LLM
    channel's AFFIRM input)."""
    return EdgeJudgment(
        evidence="ev",
        hypothesis=hyp,
        sign=EdgeSign.REFUTES,
        strength=0.9,
        opinion=Opinion(belief=0.9, disbelief=0.0, uncertainty=0.1),
        positive=0,
        negative=6,
        abstained=0,
        n_samples=6,
        sign_stable=True,
    )


def test_default_gate_authorises_when_llm_and_symbolic_affirm() -> None:
    # The whole point of W3 option (a): with the symbolic producer wired, DEFAULT_GATE
    # ({LLM, SYMBOLIC} required) can finally AUTHORISE an automated flip — no longer dead.
    symbolic = symbolic_channel_for(_q((Atom("h", True),), (Atom("h", False),)))  # AFFIRM
    decision = authorise_from_panel(
        [_stable_refuter("h")], hypothesis="h", symbolic=symbolic, gate=DEFAULT_GATE
    )
    assert decision.outcome is GateOutcome.AUTHORISED
    assert decision.authorised


def test_default_gate_symbolic_dissent_vetoes() -> None:
    # A symbolic DISSENT (the logic shows consistency where the LLM saw a contradiction) vetoes
    # even though the LLM channel affirms — the guard the symbolic channel exists for (§13).
    symbolic = symbolic_channel_for(_q((Atom("h", True),), (Atom("h", True),)))  # DISSENT
    decision = authorise_from_panel(
        [_stable_refuter("h")], hypothesis="h", symbolic=symbolic, gate=DEFAULT_GATE
    )
    assert decision.outcome is GateOutcome.WITHHELD
    assert any("symbolic" in r for r in decision.reasons)


def test_default_gate_symbolic_abstain_still_withholds() -> None:
    # An unrelated/insufficient symbolic signal abstains; SYMBOLIC is *required*, so the flip is
    # withheld — the conservative behaviour preserved when the logic cannot speak.
    symbolic = symbolic_channel_for(_q((Atom("h", True),), (Atom("z", False),)))  # ABSTAIN
    decision = authorise_from_panel(
        [_stable_refuter("h")], hypothesis="h", symbolic=symbolic, gate=DEFAULT_GATE
    )
    assert decision.outcome is GateOutcome.WITHHELD


def test_default_gate_without_symbolic_producer_is_the_safe_default() -> None:
    # Sanity / regression: with no symbolic signal supplied (the pre-W3 default), DEFAULT_GATE
    # still withholds — a consumer that has *not* built the sub-region keeps the safe default.
    decision = authorise_from_panel([_stable_refuter("h")], hypothesis="h", gate=DEFAULT_GATE)
    assert decision.outcome is GateOutcome.WITHHELD
    assert any("symbolic" in r for r in decision.reasons)
