"""Unit tests for conditional source credibility (Phase 2, G2.6).

DB-free: the pure §9.1 computation — the epistemic-class gate, the interest modifier, and
the derived (never stored) effective credibility. The use-time graph read
(``effective_credibility_of``) runs against live AGE in ``tests/integration/test_credibility.py``.
"""

import pytest

from iknos.core.credibility import effective_credibility, interest_modifier
from iknos.types.epistemic import EpistemicClass
from iknos.types.governance import InterestAlignment

# --- interest modifier (gated by epistemic class) ---


@pytest.mark.parametrize("alignment", list(InterestAlignment))
def test_observation_is_interest_independent(alignment):
    # Gate 0: credibility on an observation does not move with interest, whatever the alignment.
    assert interest_modifier(EpistemicClass.OBSERVATION, alignment) == 1.0


def test_judgement_applies_full_modifier():
    assert interest_modifier(EpistemicClass.JUDGEMENT, InterestAlignment.SELF_SERVING) < 1.0
    assert interest_modifier(EpistemicClass.JUDGEMENT, InterestAlignment.AGAINST_INTEREST) > 1.0
    assert interest_modifier(EpistemicClass.JUDGEMENT, InterestAlignment.NEUTRAL) == 1.0
    assert interest_modifier(EpistemicClass.JUDGEMENT, InterestAlignment.UNKNOWN) == 1.0


def test_testimony_is_interest_weighted_like_judgement():
    assert interest_modifier(EpistemicClass.TESTIMONY, InterestAlignment.SELF_SERVING) < 1.0


# --- effective credibility (derived, clamped to [0, 1]) ---


def test_observation_credibility_is_box_reliability_regardless_of_alignment():
    for alignment in InterestAlignment:
        c = effective_credibility(0.7, EpistemicClass.OBSERVATION, alignment)
        assert c == pytest.approx(0.7)


def test_self_serving_judgement_is_discounted():
    base = effective_credibility(0.8, EpistemicClass.JUDGEMENT, InterestAlignment.NEUTRAL)
    discounted = effective_credibility(
        0.8, EpistemicClass.JUDGEMENT, InterestAlignment.SELF_SERVING
    )
    assert discounted < base


def test_against_interest_judgement_is_boosted_and_clamped():
    # The boost can drive a reliable source to the [0, 1] ceiling — an admission against
    # interest is maximally credible; the clamp absorbs the overshoot.
    c = effective_credibility(0.9, EpistemicClass.JUDGEMENT, InterestAlignment.AGAINST_INTEREST)
    assert c == 1.0


def test_unknown_alignment_defaults_to_box_reliability():
    # No alignment pass judged the claim → identity modifier → reliability passes through.
    assert effective_credibility(0.6, EpistemicClass.JUDGEMENT) == pytest.approx(0.6)


def test_credibility_never_exceeds_one_or_drops_below_zero():
    for ec in EpistemicClass:
        for al in InterestAlignment:
            c = effective_credibility(1.0, ec, al)
            assert 0.0 <= c <= 1.0


def test_out_of_range_reliability_raises():
    with pytest.raises(ValueError):
        effective_credibility(1.5, EpistemicClass.OBSERVATION)
    with pytest.raises(ValueError):
        effective_credibility(-0.1, EpistemicClass.OBSERVATION)
