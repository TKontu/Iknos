"""G4.3 slice 1 — the subjective-logic confidence-scoring core (§8(c), steps 3–4).

Mirrors ``test_qbaf_semantics.py`` / ``test_confidence_semiring.py``: the **fusion decision
is made numerically, before trusting the pipeline**. The headline shows cumulative and
averaging fusion diverge on *correlated* evidence — averaging is **idempotent** (N copies of
one judgment fuse back to that judgment, certainty unchanged) while cumulative **accrues**
certainty as if the copies were independent. That divergence *is* the epistemic choice (§8):
averaging is the conservative default under the standing §13 correlated-LLM-error risk;
cumulative is retained at the seam for genuinely independent judges. The rest pin the
subjective-logic properties the pipeline relies on (validity, projection, discounting,
fusion neutrality/commutativity).
"""

import pytest

from iknos.core.subjective_logic import (
    AVERAGING,
    CUMULATIVE,
    DEFAULT_FUSION,
    Fusion,
    Opinion,
    averaging_fuse,
    cumulative_fuse,
    discount,
    fuse,
    opinion_from_evidence,
    vacuous,
)

BOTH: tuple[Fusion, ...] = (CUMULATIVE, AVERAGING)


# --------------------------------------------------------------------------------------------
# The decision: cumulative ACCRUES vs averaging is IDEMPOTENT on correlated evidence
# --------------------------------------------------------------------------------------------


def test_decision_fixture_averaging_idempotent_cumulative_accrues_on_correlated() -> None:
    """The headline (the §8 epistemic choice, demonstrated numerically).

    One weak supporting judgment — 2 of 3 samples said "supports" — encoded as an opinion. Now
    imagine the *same* judgment arrives three times (three correlated LLM calls, the §13 risk):

    - **Averaging fusion is idempotent**: fusing the opinion with copies of itself returns the
      *same* opinion. Uncertainty does not shrink, belief does not grow — correlated evidence
      manufactures no certainty. This is why it is the conservative default.
    - **Cumulative fusion accrues**: it treats each copy as independent, so uncertainty shrinks
      and belief climbs with every copy — exactly the false confidence §13 warns about when the
      judges are correlated.

    The two *diverge* on the same input, which is why §8 forces the choice with a fixture.
    """
    weak = opinion_from_evidence(positive=2, negative=1)  # 2/3 samples supported

    avg = fuse([weak, weak, weak], fusion=AVERAGING)
    cum = fuse([weak, weak, weak], fusion=CUMULATIVE)

    # Averaging: three correlated copies == one judgment. No manufactured certainty.
    assert avg.belief == pytest.approx(weak.belief)
    assert avg.uncertainty == pytest.approx(weak.uncertainty)
    assert avg.projected_probability == pytest.approx(weak.projected_probability)

    # Cumulative: uncertainty collapses, belief climbs — accrual as if independent.
    assert cum.uncertainty < weak.uncertainty
    assert cum.belief > weak.belief
    assert cum.projected_probability > weak.projected_probability


def test_default_fusion_is_averaging_the_conservative_choice() -> None:
    """Recorded eyes-open, like ``DEFAULT_SEMANTICS = DF_QUAD``: the default cannot inflate."""
    assert DEFAULT_FUSION is AVERAGING


def test_cumulative_accrues_certainty_with_independent_supporters() -> None:
    """The flip side of the decision: when judgments really *are* independent, cumulative is the
    right tool — two independent weak supports legitimately raise certainty. The seam keeps it
    available for decorrelated (varied-model) judges."""
    a = opinion_from_evidence(positive=2, negative=1)
    b = opinion_from_evidence(positive=2, negative=1)
    fused = cumulative_fuse(a, b)
    assert fused.uncertainty < a.uncertainty
    assert fused.belief > a.belief


# --------------------------------------------------------------------------------------------
# Opinion validity + projection
# --------------------------------------------------------------------------------------------


def test_opinion_masses_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        Opinion(belief=0.5, disbelief=0.5, uncertainty=0.5, base_rate=0.5)


def test_opinion_rejects_out_of_range_components() -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        Opinion(belief=-0.1, disbelief=0.0, uncertainty=1.1, base_rate=0.5)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        Opinion(belief=0.0, disbelief=0.0, uncertainty=1.0, base_rate=1.5)


def test_projected_probability_interpolates_base_rate_over_uncertainty() -> None:
    """P = b + a·u: a vacuous opinion projects to the base rate; a dogmatic one to its belief."""
    assert vacuous(base_rate=0.3).projected_probability == pytest.approx(0.3)
    dogmatic = Opinion(belief=0.8, disbelief=0.2, uncertainty=0.0, base_rate=0.5)
    assert dogmatic.projected_probability == pytest.approx(0.8)
    mixed = Opinion(belief=0.5, disbelief=0.1, uncertainty=0.4, base_rate=0.5)
    assert mixed.projected_probability == pytest.approx(0.5 + 0.5 * 0.4)


def test_vacuous_is_total_uncertainty() -> None:
    v = vacuous()
    assert (v.belief, v.disbelief, v.uncertainty) == (0.0, 0.0, 1.0)
    assert v.base_rate == 0.5


# --------------------------------------------------------------------------------------------
# Multi-sample consistency → opinion (step 1's encoding)
# --------------------------------------------------------------------------------------------


def test_opinion_from_evidence_maps_agreement_to_belief() -> None:
    """k positive of N samples → Beta/binomial opinion with a non-informative prior weight W.
    Belief tracks the positive fraction; uncertainty is the prior's share of the total mass."""
    op = opinion_from_evidence(positive=3, negative=1, prior_weight=2.0)
    total = 3 + 1 + 2.0
    assert op.belief == pytest.approx(3 / total)
    assert op.disbelief == pytest.approx(1 / total)
    assert op.uncertainty == pytest.approx(2.0 / total)


def test_opinion_from_evidence_more_samples_shrink_uncertainty() -> None:
    """Same 3:1 ratio but more total observations ⇒ less uncertainty (consistency = certainty)."""
    few = opinion_from_evidence(positive=3, negative=1)
    many = opinion_from_evidence(positive=30, negative=10)
    assert many.uncertainty < few.uncertainty
    assert many.belief > few.belief


def test_opinion_from_evidence_no_observations_is_vacuous() -> None:
    """Zero samples ⇒ all mass on the prior ⇒ a vacuous opinion (projects to the base rate)."""
    op = opinion_from_evidence(positive=0, negative=0)
    assert op.uncertainty == pytest.approx(1.0)
    assert op.projected_probability == pytest.approx(op.base_rate)


def test_opinion_from_evidence_rejects_negative_counts_and_bad_prior() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        opinion_from_evidence(positive=-1, negative=0)
    with pytest.raises(ValueError, match="prior_weight"):
        opinion_from_evidence(positive=1, negative=0, prior_weight=0.0)


# --------------------------------------------------------------------------------------------
# Source-reliability discounting (step 3 — the §8 ↔ §9.1 credibility seam)
# --------------------------------------------------------------------------------------------


def test_discount_full_reliability_is_identity() -> None:
    op = opinion_from_evidence(positive=3, negative=1)
    d = discount(op, 1.0)  # identity up to floating point (uncertainty is recomputed as 1−(b+d))
    assert d.belief == pytest.approx(op.belief)
    assert d.disbelief == pytest.approx(op.disbelief)
    assert d.uncertainty == pytest.approx(op.uncertainty)


def test_discount_zero_reliability_is_vacuous() -> None:
    """A wholly unreliable source contributes no belief or disbelief — only uncertainty."""
    op = opinion_from_evidence(positive=3, negative=1)
    d = discount(op, 0.0)
    assert (d.belief, d.disbelief) == (0.0, 0.0)
    assert d.uncertainty == pytest.approx(1.0)


def test_discount_scales_belief_and_disbelief_preserving_validity() -> None:
    op = Opinion(belief=0.6, disbelief=0.2, uncertainty=0.2, base_rate=0.5)
    d = discount(op, 0.5)
    assert d.belief == pytest.approx(0.3)
    assert d.disbelief == pytest.approx(0.1)
    assert d.uncertainty == pytest.approx(1.0 - 0.5 * (0.6 + 0.2))
    assert d.base_rate == op.base_rate  # unchanged


def test_discount_rejects_out_of_range_reliability() -> None:
    op = vacuous()
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        discount(op, 1.5)


# --------------------------------------------------------------------------------------------
# Fusion operators — neutrality, commutativity, idempotency, bounds
# --------------------------------------------------------------------------------------------


def test_vacuous_is_neutral_for_cumulative_fusion() -> None:
    """A vacuous opinion (no evidence) is the identity element of **cumulative** fusion — an
    independent non-judging source adds nothing, so it cannot move the result."""
    op = opinion_from_evidence(positive=3, negative=1)
    fused = cumulative_fuse(op, vacuous(base_rate=op.base_rate))
    assert fused.belief == pytest.approx(op.belief)
    assert fused.disbelief == pytest.approx(op.disbelief)
    assert fused.uncertainty == pytest.approx(op.uncertainty)


def test_averaging_with_vacuous_dilutes_toward_uncertainty() -> None:
    """Averaging fusion weights each opinion by its *uncertainty*, so a vacuous opinion (maximal
    uncertainty) is **not** neutral — it pulls the fused result toward uncertainty. This is the
    intended conservative behavior: an abstaining (or fully source-discounted) judge *raises*
    uncertainty rather than being silently ignored, unlike under cumulative fusion."""
    op = opinion_from_evidence(positive=3, negative=1)  # (0.5, 1/6, 1/3)
    fused = averaging_fuse(op, vacuous(base_rate=op.base_rate))
    assert fused.uncertainty > op.uncertainty  # diluted, not ignored
    assert fused.belief < op.belief
    assert fused.belief == pytest.approx(0.375)
    assert fused.disbelief == pytest.approx(0.125)
    assert fused.uncertainty == pytest.approx(0.5)


@pytest.mark.parametrize("fusion", BOTH, ids=lambda f: f.name)
def test_fusion_is_commutative(fusion: Fusion) -> None:
    a = opinion_from_evidence(positive=3, negative=1)
    b = opinion_from_evidence(positive=1, negative=2)
    ab = fusion.fuse_pair(a, b)
    ba = fusion.fuse_pair(b, a)
    assert ab.belief == pytest.approx(ba.belief)
    assert ab.disbelief == pytest.approx(ba.disbelief)
    assert ab.uncertainty == pytest.approx(ba.uncertainty)


def test_averaging_idempotent_cumulative_not() -> None:
    op = opinion_from_evidence(positive=3, negative=1)
    assert averaging_fuse(op, op).belief == pytest.approx(op.belief)
    assert averaging_fuse(op, op).uncertainty == pytest.approx(op.uncertainty)
    assert cumulative_fuse(op, op).uncertainty < op.uncertainty


def test_averaging_does_not_reduce_uncertainty_below_inputs() -> None:
    """Averaging fusion never produces *more* certainty than its most-certain input — the
    property that makes it safe under correlation."""
    a = opinion_from_evidence(positive=3, negative=1)  # lower uncertainty
    b = opinion_from_evidence(positive=1, negative=0)  # higher uncertainty
    fused = averaging_fuse(a, b)
    assert fused.uncertainty >= min(a.uncertainty, b.uncertainty) - 1e-9


@pytest.mark.parametrize("fusion", BOTH, ids=lambda f: f.name)
def test_fuse_single_opinion_is_identity(fusion: Fusion) -> None:
    op = opinion_from_evidence(positive=3, negative=1)
    assert fuse([op], fusion=fusion) == op


def test_fuse_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        fuse([], fusion=AVERAGING)


def test_fuse_rejects_mismatched_base_rates() -> None:
    """Fusion combines judgments of the *same* proposition, so the base rate is shared; a
    mismatch is a caller bug (mixing propositions), surfaced rather than silently averaged."""
    a = opinion_from_evidence(positive=1, negative=0, base_rate=0.5)
    b = opinion_from_evidence(positive=1, negative=0, base_rate=0.2)
    with pytest.raises(ValueError, match="base_rate"):
        fuse([a, b], fusion=AVERAGING)


@pytest.mark.parametrize("fusion", BOTH, ids=lambda f: f.name)
def test_fused_opinion_is_valid(fusion: Fusion) -> None:
    """Whatever the inputs, the fused opinion is a valid opinion (masses in [0,1], sum to 1) —
    the Opinion constructor would raise otherwise, so reaching here is the assertion."""
    ops = [
        opinion_from_evidence(positive=p, negative=n) for p, n in [(3, 1), (0, 2), (5, 5), (1, 0)]
    ]
    fused = fuse(ops, fusion=fusion)
    assert fused.belief + fused.disbelief + fused.uncertainty == pytest.approx(1.0)


@pytest.mark.parametrize("fusion", BOTH, ids=lambda f: f.name)
def test_both_dogmatic_fusion_averages_belief(fusion: Fusion) -> None:
    """Two zero-uncertainty (dogmatic) opinions have no uncertainty mass to weight by; both
    fusions fall back to the equal-weight average of their beliefs (the documented limit)."""
    a = Opinion(belief=0.8, disbelief=0.2, uncertainty=0.0, base_rate=0.5)
    b = Opinion(belief=0.4, disbelief=0.6, uncertainty=0.0, base_rate=0.5)
    fused = fusion.fuse_pair(a, b)
    assert fused.belief == pytest.approx(0.6)
    assert fused.disbelief == pytest.approx(0.4)
    assert fused.uncertainty == pytest.approx(0.0)
