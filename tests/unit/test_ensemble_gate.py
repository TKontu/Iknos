"""G4.5 slice 1 — the §7.2 ensemble gate (the refuted-flip authoriser).

Mirrors ``test_qbaf_adjudication.py`` / ``test_subjective_logic.py``: the **decision fixture**
first (the conservative unanimity-veto gate vs a majority vote — the choice recorded eyes-open),
then the pure decision algebra (dissent veto, required-channel floor, abstain handling), the LLM-
channel bridge from the produced :class:`~iknos.core.edge_judge.EdgeJudgment`s, and the
safe-by-default behaviour while the symbolic/temporal producers are unwired.
"""

import pytest

from iknos.core.edge_judge import EdgeJudgment
from iknos.core.ensemble_gate import (
    DEFAULT_GATE,
    LLM_ONLY_GATE,
    STRICT_GATE,
    ChannelStance,
    GateChannel,
    GateOutcome,
    abstaining,
    affirming,
    authorise,
    authorise_from_panel,
    authorised_hypotheses,
    dissenting,
    llm_channel,
    symbolic_channel,
    temporal_channel,
)
from iknos.core.subjective_logic import Opinion
from iknos.types.edges import EdgeSign


def _judgment(
    evidence: str,
    hypothesis: str,
    *,
    sign: EdgeSign = EdgeSign.REFUTES,
    strength: float = 0.8,
    sign_stable: bool = True,
) -> EdgeJudgment:
    """A hand-built :class:`EdgeJudgment` (only sign/strength/sign_stable matter to the gate)."""
    return EdgeJudgment(
        evidence=evidence,
        hypothesis=hypothesis,
        sign=sign,
        strength=strength,
        opinion=Opinion(belief=strength, disbelief=0.0, uncertainty=1.0 - strength),
        positive=5,
        negative=0,
        abstained=0,
        n_samples=5,
        sign_stable=sign_stable,
    )


# --------------------------------------------------------------------------------------------
# The decision fixture — the conservative gate vs a majority vote (recorded eyes-open)
# --------------------------------------------------------------------------------------------


def test_decision_fixture_dissent_vetoes_where_majority_would_authorise() -> None:
    """The headline §7.2 choice: two channels affirm, one dissents.

    A **majority vote** (2 affirm > 1 dissent) would authorise the flip; the conservative
    ``DEFAULT_GATE`` **withholds** it — a single dissent is a §13 finding, never out-voted. This is
    the failure mode the gate exists to prevent: a confident-but-wrong LLM channel carrying a flip
    past a dissenting symbolic/temporal check.
    """
    signals = [
        affirming(GateChannel.LLM),
        affirming(GateChannel.SYMBOLIC),
        dissenting(GateChannel.TEMPORAL, "timeline forbids the contradiction"),
    ]
    decision = authorise(signals, gate=DEFAULT_GATE)
    assert decision.outcome is GateOutcome.WITHHELD
    assert not decision.authorised
    assert decision.is_finding
    # 2-of-3 affirm — a majority rule would have flipped; the dissent reason is surfaced, not lost.
    affirmed = sum(1 for s in signals if s.stance is ChannelStance.AFFIRM)
    assert affirmed == 2
    assert any("temporal dissents" in r for r in decision.reasons)


# --------------------------------------------------------------------------------------------
# The decision algebra — authorise()
# --------------------------------------------------------------------------------------------


def test_all_required_affirm_no_dissent_authorises() -> None:
    """Default gate: LLM + SYMBOLIC affirm, TEMPORAL abstains (not required) ⇒ AUTHORISED."""
    decision = authorise(
        [
            affirming(GateChannel.LLM),
            affirming(GateChannel.SYMBOLIC),
            abstaining(GateChannel.TEMPORAL),
        ]
    )
    assert decision.authorised
    assert decision.outcome is GateOutcome.AUTHORISED
    assert decision.reasons == ()
    assert not decision.is_finding


def test_any_dissent_vetoes_even_with_required_affirmed() -> None:
    """A dissent on a *non-required* channel still vetoes — the universal-veto rule."""
    decision = authorise(
        [
            affirming(GateChannel.LLM),
            affirming(GateChannel.SYMBOLIC),
            dissenting(GateChannel.TEMPORAL),
        ]
    )
    assert decision.is_finding
    assert any("temporal dissents" in r for r in decision.reasons)


def test_required_channel_abstains_withholds() -> None:
    """A required channel that abstains fails the floor (the ensemble did not affirm)."""
    decision = authorise(
        [
            affirming(GateChannel.LLM),
            abstaining(GateChannel.SYMBOLIC, "clingo not wired"),
        ]
    )
    assert decision.is_finding
    assert any("symbolic" in r and "abstained" in r for r in decision.reasons)


def test_missing_required_channel_is_treated_as_abstain() -> None:
    """A required channel that did not report at all withholds — not an error, an abstention."""
    decision = authorise([affirming(GateChannel.LLM)])  # SYMBOLIC absent
    assert decision.is_finding
    assert any("symbolic" in r and "did not report" in r for r in decision.reasons)


def test_non_required_abstain_is_ignored() -> None:
    """A non-required channel abstaining does not block (only required channels must affirm)."""
    decision = authorise(
        [
            affirming(GateChannel.LLM),
            affirming(GateChannel.SYMBOLIC),
            abstaining(GateChannel.TEMPORAL),
        ]
    )
    assert decision.authorised


def test_non_required_affirm_is_welcome_not_necessary() -> None:
    """An extra (non-required) affirmation is fine; it neither blocks nor is needed."""
    decision = authorise(
        [
            affirming(GateChannel.LLM),
            affirming(GateChannel.SYMBOLIC),
            affirming(GateChannel.TEMPORAL),
        ]
    )
    assert decision.authorised


def test_duplicate_channel_signal_raises() -> None:
    """Two signals for one channel is a caller bug — surfaced, not silently resolved."""
    with pytest.raises(ValueError, match="duplicate signal for channel"):
        authorise([affirming(GateChannel.LLM), dissenting(GateChannel.LLM)])


def test_signals_ordered_deterministically_by_channel() -> None:
    """The decision orders signals by the channel enum, regardless of input order (replay, §10)."""
    decision = authorise(
        [
            affirming(GateChannel.TEMPORAL),
            affirming(GateChannel.SYMBOLIC),
            affirming(GateChannel.LLM),
        ]
    )
    assert tuple(s.channel for s in decision.signals) == (
        GateChannel.LLM,
        GateChannel.SYMBOLIC,
        GateChannel.TEMPORAL,
    )


# --------------------------------------------------------------------------------------------
# Gate-policy variants (the retained seams)
# --------------------------------------------------------------------------------------------


def test_llm_only_gate_authorises_on_llm_alone() -> None:
    """``LLM_ONLY_GATE`` authorises when only the LLM channel affirms (the MVP seam)."""
    decision = authorise(
        [
            affirming(GateChannel.LLM),
            abstaining(GateChannel.SYMBOLIC),
            abstaining(GateChannel.TEMPORAL),
        ],
        gate=LLM_ONLY_GATE,
    )
    assert decision.authorised


def test_llm_only_gate_still_dissent_vetoed() -> None:
    """Even the loose gate vetoes on a dissent — the universal rule is not a policy knob."""
    decision = authorise(
        [affirming(GateChannel.LLM), dissenting(GateChannel.SYMBOLIC)],
        gate=LLM_ONLY_GATE,
    )
    assert decision.is_finding


def test_strict_gate_requires_temporal_to_affirm() -> None:
    """``STRICT_GATE`` requires all three; a temporal abstention now withholds."""
    base = [affirming(GateChannel.LLM), affirming(GateChannel.SYMBOLIC)]
    assert authorise([*base, abstaining(GateChannel.TEMPORAL)], gate=STRICT_GATE).is_finding
    assert authorise([*base, affirming(GateChannel.TEMPORAL)], gate=STRICT_GATE).authorised


# --------------------------------------------------------------------------------------------
# The LLM-channel bridge — llm_channel()
# --------------------------------------------------------------------------------------------


def test_llm_channel_affirms_on_stable_refuter() -> None:
    """A stable ``REFUTES`` judgment ⇒ the LLM channel AFFIRMS, naming the strongest edge."""
    sig = llm_channel([_judgment("e1", "h1", strength=0.7)])
    assert sig.channel is GateChannel.LLM
    assert sig.stance is ChannelStance.AFFIRM
    assert "e1" in sig.detail


def test_llm_channel_abstains_when_all_refuters_unstable() -> None:
    """Every refuting edge sign-unstable (panel split) ⇒ ABSTAIN — the §13 finding to clear."""
    sig = llm_channel([_judgment("e1", "h1", sign_stable=False)])
    assert sig.stance is ChannelStance.ABSTAIN
    assert "unstable" in sig.detail


def test_llm_channel_abstains_with_no_refuter() -> None:
    """Only ``SUPPORTS`` edges ⇒ no refutation to affirm ⇒ ABSTAIN (absence is not dissent)."""
    sig = llm_channel([_judgment("e1", "h1", sign=EdgeSign.SUPPORTS)])
    assert sig.stance is ChannelStance.ABSTAIN


def test_llm_channel_one_stable_refuter_among_unstable_affirms() -> None:
    """A single stable refuter is enough to affirm even alongside unstable ones."""
    sig = llm_channel(
        [
            _judgment("e1", "h1", sign_stable=False),
            _judgment("e2", "h1", strength=0.6, sign_stable=True),
        ]
    )
    assert sig.stance is ChannelStance.AFFIRM
    assert "e2" in sig.detail  # the stable one is named, not the unstable


def test_llm_channel_strongest_stable_refuter_is_reported() -> None:
    """Among stable refuters the detail names the strongest by strength."""
    sig = llm_channel(
        [
            _judgment("weak", "h1", strength=0.4),
            _judgment("strong", "h1", strength=0.9),
        ]
    )
    assert sig.stance is ChannelStance.AFFIRM
    assert "strong" in sig.detail


def test_llm_channel_filters_by_hypothesis() -> None:
    """The optional ``hypothesis`` filter restricts to refuters bearing on that hypothesis."""
    judgments = [_judgment("e1", "other"), _judgment("e2", "h1")]
    assert llm_channel(judgments, hypothesis="h1").stance is ChannelStance.AFFIRM
    assert "e2" in llm_channel(judgments, hypothesis="h1").detail
    # A hypothesis with no refuter of its own abstains even though the set has refuters.
    assert llm_channel(judgments, hypothesis="none").stance is ChannelStance.ABSTAIN


# --------------------------------------------------------------------------------------------
# The unwired channel seams — symbolic_channel() / temporal_channel()
# --------------------------------------------------------------------------------------------


def test_symbolic_and_temporal_channels_abstain_by_default() -> None:
    """The unwired producers ABSTAIN on the right channel (a present-but-silent seam)."""
    sym = symbolic_channel()
    tmp = temporal_channel()
    assert (sym.channel, sym.stance) == (GateChannel.SYMBOLIC, ChannelStance.ABSTAIN)
    assert (tmp.channel, tmp.stance) == (GateChannel.TEMPORAL, ChannelStance.ABSTAIN)


# --------------------------------------------------------------------------------------------
# authorise_from_panel() — assemble the three channels in one call
# --------------------------------------------------------------------------------------------


def test_authorise_from_panel_withholds_until_symbolic_wired() -> None:
    """**Safe-by-default**: a stable refuting panel alone does *not* authorise under the default
    gate — the unwired (abstaining) symbolic channel is required, so the flip is surfaced as a
    finding for expert review, never auto-persisted (§7.2 + principle 6)."""
    decision = authorise_from_panel([_judgment("e1", "h1")], hypothesis="h1")
    assert decision.is_finding
    assert any("symbolic" in r for r in decision.reasons)


def test_authorise_from_panel_authorises_with_explicit_symbolic_affirm() -> None:
    """Supplying an affirming symbolic signal (the producer's future output) authorises the flip."""
    decision = authorise_from_panel(
        [_judgment("e1", "h1")],
        hypothesis="h1",
        symbolic=affirming(GateChannel.SYMBOLIC, "logically inconsistent"),
    )
    assert decision.authorised


def test_authorise_from_panel_llm_only_gate_authorises_on_panel() -> None:
    """Under the MVP ``LLM_ONLY_GATE`` a stable refuting panel authorises with no other producer."""
    decision = authorise_from_panel([_judgment("e1", "h1")], hypothesis="h1", gate=LLM_ONLY_GATE)
    assert decision.authorised


def test_authorise_from_panel_temporal_dissent_vetoes() -> None:
    """A temporal dissent vetoes even when the LLM + symbolic channels would authorise."""
    decision = authorise_from_panel(
        [_judgment("e1", "h1")],
        hypothesis="h1",
        symbolic=affirming(GateChannel.SYMBOLIC),
        temporal=dissenting(GateChannel.TEMPORAL, "refuting fact predates the claim"),
    )
    assert decision.is_finding


# --------------------------------------------------------------------------------------------
# authorised_hypotheses() — the batch consumer form
# --------------------------------------------------------------------------------------------


def test_authorised_hypotheses_decides_per_hypothesis_in_sorted_order() -> None:
    """One decision per structurally-refuted hypothesis, keyed by id, sorted (replay, §10)."""
    judged = {
        "h2": [_judgment("e1", "h2")],
        "h1": [_judgment("e2", "h1", sign=EdgeSign.SUPPORTS)],  # no refuter → LLM abstains
    }
    decisions = authorised_hypotheses(
        judged,
        structurally_refuted=["h2", "h1"],
        # Both withhold under the default gate (symbolic unwired); h1 additionally lacks a refuter.
    )
    assert list(decisions) == ["h1", "h2"]  # sorted
    assert all(d.is_finding for d in decisions.values())


def test_authorised_hypotheses_respects_per_hypothesis_symbolic_signal() -> None:
    """A per-hypothesis affirming symbolic signal authorises just that hypothesis's flip."""
    judged = {"h1": [_judgment("e1", "h1")], "h2": [_judgment("e2", "h2")]}
    decisions = authorised_hypotheses(
        judged,
        structurally_refuted=["h1", "h2"],
        symbolic={"h1": affirming(GateChannel.SYMBOLIC)},  # h2 left to the abstaining default
    )
    assert decisions["h1"].authorised
    assert decisions["h2"].is_finding


def test_authorised_hypotheses_missing_judged_entry_abstains() -> None:
    """A structurally-refuted hypothesis with no produced judgments abstains the LLM channel."""
    decisions = authorised_hypotheses(
        {},
        structurally_refuted=["h1"],
        symbolic={"h1": affirming(GateChannel.SYMBOLIC)},
        gate=LLM_ONLY_GATE,
    )
    # LLM_ONLY_GATE, but no refuter ⇒ LLM abstains ⇒ withheld.
    assert decisions["h1"].is_finding
