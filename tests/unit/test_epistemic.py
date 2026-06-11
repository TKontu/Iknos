"""Unit tests for the epistemic-field vocabulary + derivations (G1.1/G1.2).

Pure: routing is exhaustive over the class vocabulary, the provisional gate matches
the band() boundary/raise convention, and enum value strings are exactly the spec
strings (a drift guided decoding would otherwise hide).
"""

import itertools

import pytest

from iknos.types.epistemic import (
    Attribution,
    Entailment,
    EpistemicClass,
    Modality,
    Polarity,
    ProvisionalReason,
    Routing,
    combine_faithfulness,
    faithfulness_from_verdict,
    provisional_reasons_for,
    route_for,
)

_LOW = ProvisionalReason.LOW_FAITHFULNESS

# --- routing (G1.2) ---


@pytest.mark.parametrize("ec", list(EpistemicClass))
def test_route_for_covers_every_class(ec: EpistemicClass) -> None:
    # No KeyError for any member → the routing map is exhaustive (fail-loud on growth).
    assert isinstance(route_for(ec), Routing)


def test_observation_routes_to_fact_others_to_judgement() -> None:
    assert route_for(EpistemicClass.OBSERVATION) is Routing.FACT
    assert route_for(EpistemicClass.TESTIMONY) is Routing.JUDGEMENT
    assert route_for(EpistemicClass.JUDGEMENT) is Routing.JUDGEMENT


# --- provisional reasons gate (R8 — faithfulness-derived reason set) ---


def test_provisional_reasons_half_open_boundary() -> None:
    assert provisional_reasons_for(0.49) == {_LOW}
    assert provisional_reasons_for(0.5) == set()  # at-threshold is NOT provisional (band() conv.)
    assert provisional_reasons_for(0.9) == set()


def test_provisional_reasons_none_faithfulness_is_empty() -> None:
    # No verifier / single-pass degraded mode: nothing to gate on — not a positive signal.
    assert provisional_reasons_for(None) == set()


def test_provisional_reasons_custom_threshold() -> None:
    assert provisional_reasons_for(0.7, threshold=0.8) == {_LOW}
    assert provisional_reasons_for(0.8, threshold=0.8) == set()


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_provisional_reasons_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        provisional_reasons_for(bad)


# --- faithfulness from the verify verdict (G1.4/G1.5) ---


def test_faithfulness_contradicted_is_zero() -> None:
    # Span asserts the opposite — never faithful, regardless of operator flags.
    assert faithfulness_from_verdict(Entailment.CONTRADICTED, True, True) == 0.0
    assert faithfulness_from_verdict(Entailment.CONTRADICTED, False, False) == 0.0


def test_faithfulness_neutral_is_low_and_provisional() -> None:
    # Unsupported / hallucinated content sits below the provisional threshold by design.
    score = faithfulness_from_verdict(Entailment.NEUTRAL, True, True)
    assert score == pytest.approx(0.30)
    assert provisional_reasons_for(score) == {_LOW}


def test_faithfulness_entailed_fully_preserved_is_one() -> None:
    score = faithfulness_from_verdict(Entailment.ENTAILED, True, True)
    assert score == pytest.approx(1.0)
    assert provisional_reasons_for(score) == set()


def test_faithfulness_polarity_drop_is_low_and_provisional() -> None:
    # A dropped negation is a sign flip — severe; quarantined below threshold.
    score = faithfulness_from_verdict(Entailment.ENTAILED, False, True)
    assert score == pytest.approx(0.40)
    assert provisional_reasons_for(score) == {_LOW}


def test_faithfulness_modality_flatten_is_medium_not_provisional() -> None:
    # A flattened hedge over-states certainty — moderate; stays above threshold.
    score = faithfulness_from_verdict(Entailment.ENTAILED, True, False)
    assert score == pytest.approx(0.70)
    assert provisional_reasons_for(score) == set()


def test_faithfulness_polarity_and_modality_drop_compounds() -> None:
    score = faithfulness_from_verdict(Entailment.ENTAILED, False, False)
    assert score == pytest.approx(0.40 * 0.70)
    assert provisional_reasons_for(score) == {_LOW}


@pytest.mark.parametrize(
    ("entailment", "pol", "mod"),
    list(itertools.product(Entailment, [True, False], [True, False])),
)
def test_faithfulness_always_in_range(entailment: Entailment, pol: bool, mod: bool) -> None:
    # Every verdict yields a value in [0, 1] that provisional_reasons_for() accepts without raising.
    score = faithfulness_from_verdict(entailment, pol, mod)
    assert 0.0 <= score <= 1.0
    assert isinstance(provisional_reasons_for(score), set)


def test_faithfulness_fail_loud_on_unknown_entailment() -> None:
    # Mirrors route_for exhaustiveness: an unmapped verdict raises rather than defaulting.
    # (A *valid* StrEnum string resolves via the dict — StrEnum members hash equal to their
    # value — so an unmapped string is the real fail-loud case.)
    with pytest.raises(KeyError):
        faithfulness_from_verdict("bogus", True, True)  # type: ignore[arg-type]


# --- combine verify × multi-sample agreement (G1.3) ---


def test_combine_agreement_one_is_identity() -> None:
    # Single-pass / N=1 (agreement 1.0) → faithfulness == the verify component, unchanged.
    assert combine_faithfulness(0.70, 1.0) == pytest.approx(0.70)
    assert combine_faithfulness(1.0, 1.0) == pytest.approx(1.0)


def test_combine_is_multiplicative() -> None:
    assert combine_faithfulness(1.0, 2 / 3) == pytest.approx(2 / 3)
    assert combine_faithfulness(0.7, 1.0) == pytest.approx(0.7)


def test_combine_unstable_but_verified_becomes_provisional() -> None:
    # Verifier passes it (1.0) but it appeared in only 1 of 3 samples → quarantined.
    score = combine_faithfulness(1.0, 1 / 3)
    assert score == pytest.approx(1 / 3)
    assert provisional_reasons_for(score) == {_LOW}


def test_combine_stable_and_verified_stays_above_threshold() -> None:
    score = combine_faithfulness(1.0, 2 / 3)
    assert provisional_reasons_for(score) == set()


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_combine_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        combine_faithfulness(bad, 1.0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        combine_faithfulness(1.0, bad)


# --- enum value strings are exactly the spec strings (guards silent drift) ---


def test_enum_value_strings_match_spec() -> None:
    assert [p.value for p in Polarity] == ["asserted", "negated"]
    assert [m.value for m in Modality] == ["categorical", "probable", "possible", "hypothesized"]
    assert [a.value for a in Attribution] == ["document", "reported-speech", "named-source"]
    assert [e.value for e in EpistemicClass] == ["observation", "testimony", "judgement"]
    assert [r.value for r in Routing] == ["fact", "judgement"]
    assert [e.value for e in Entailment] == ["entailed", "neutral", "contradicted"]
    assert [p.value for p in ProvisionalReason] == [
        "low_faithfulness",
        "unresolved_reference",
        "uninferred_budget",
    ]
