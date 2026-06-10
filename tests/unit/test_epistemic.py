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
    Routing,
    faithfulness_from_verdict,
    is_provisional,
    route_for,
)

# --- routing (G1.2) ---


@pytest.mark.parametrize("ec", list(EpistemicClass))
def test_route_for_covers_every_class(ec: EpistemicClass) -> None:
    # No KeyError for any member → the routing map is exhaustive (fail-loud on growth).
    assert isinstance(route_for(ec), Routing)


def test_observation_routes_to_fact_others_to_judgement() -> None:
    assert route_for(EpistemicClass.OBSERVATION) is Routing.FACT
    assert route_for(EpistemicClass.TESTIMONY) is Routing.JUDGEMENT
    assert route_for(EpistemicClass.JUDGEMENT) is Routing.JUDGEMENT


# --- provisional gate (landed for G1.4/G1.5/G1.6; not called in G1.1) ---


def test_is_provisional_half_open_boundary() -> None:
    assert is_provisional(0.49) is True
    assert is_provisional(0.5) is False  # at-threshold is NOT provisional (band() convention)
    assert is_provisional(0.9) is False


def test_is_provisional_custom_threshold() -> None:
    assert is_provisional(0.7, threshold=0.8) is True
    assert is_provisional(0.8, threshold=0.8) is False


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_is_provisional_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        is_provisional(bad)


# --- faithfulness from the verify verdict (G1.4/G1.5) ---


def test_faithfulness_contradicted_is_zero() -> None:
    # Span asserts the opposite — never faithful, regardless of operator flags.
    assert faithfulness_from_verdict(Entailment.CONTRADICTED, True, True) == 0.0
    assert faithfulness_from_verdict(Entailment.CONTRADICTED, False, False) == 0.0


def test_faithfulness_neutral_is_low_and_provisional() -> None:
    # Unsupported / hallucinated content sits below the provisional threshold by design.
    score = faithfulness_from_verdict(Entailment.NEUTRAL, True, True)
    assert score == pytest.approx(0.30)
    assert is_provisional(score) is True


def test_faithfulness_entailed_fully_preserved_is_one() -> None:
    score = faithfulness_from_verdict(Entailment.ENTAILED, True, True)
    assert score == pytest.approx(1.0)
    assert is_provisional(score) is False


def test_faithfulness_polarity_drop_is_low_and_provisional() -> None:
    # A dropped negation is a sign flip — severe; quarantined below threshold.
    score = faithfulness_from_verdict(Entailment.ENTAILED, False, True)
    assert score == pytest.approx(0.40)
    assert is_provisional(score) is True


def test_faithfulness_modality_flatten_is_medium_not_provisional() -> None:
    # A flattened hedge over-states certainty — moderate; stays above threshold.
    score = faithfulness_from_verdict(Entailment.ENTAILED, True, False)
    assert score == pytest.approx(0.70)
    assert is_provisional(score) is False


def test_faithfulness_polarity_and_modality_drop_compounds() -> None:
    score = faithfulness_from_verdict(Entailment.ENTAILED, False, False)
    assert score == pytest.approx(0.40 * 0.70)
    assert is_provisional(score) is True


@pytest.mark.parametrize(
    ("entailment", "pol", "mod"),
    list(itertools.product(Entailment, [True, False], [True, False])),
)
def test_faithfulness_always_in_range(entailment: Entailment, pol: bool, mod: bool) -> None:
    # Every verdict yields a value in [0, 1] that is_provisional() accepts without raising.
    score = faithfulness_from_verdict(entailment, pol, mod)
    assert 0.0 <= score <= 1.0
    assert isinstance(is_provisional(score), bool)


def test_faithfulness_fail_loud_on_unknown_entailment() -> None:
    # Mirrors route_for exhaustiveness: an unmapped verdict raises rather than defaulting.
    # (A *valid* StrEnum string resolves via the dict — StrEnum members hash equal to their
    # value — so an unmapped string is the real fail-loud case.)
    with pytest.raises(KeyError):
        faithfulness_from_verdict("bogus", True, True)  # type: ignore[arg-type]


# --- enum value strings are exactly the spec strings (guards silent drift) ---


def test_enum_value_strings_match_spec() -> None:
    assert [p.value for p in Polarity] == ["asserted", "negated"]
    assert [m.value for m in Modality] == ["categorical", "probable", "possible", "hypothesized"]
    assert [a.value for a in Attribution] == ["document", "reported-speech", "named-source"]
    assert [e.value for e in EpistemicClass] == ["observation", "testimony", "judgement"]
    assert [r.value for r in Routing] == ["fact", "judgement"]
    assert [e.value for e in Entailment] == ["entailed", "neutral", "contradicted"]
