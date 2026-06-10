"""Unit tests for the epistemic-field vocabulary + derivations (G1.1/G1.2).

Pure: routing is exhaustive over the class vocabulary, the provisional gate matches
the band() boundary/raise convention, and enum value strings are exactly the spec
strings (a drift guided decoding would otherwise hide).
"""

import pytest

from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
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


# --- enum value strings are exactly the spec strings (guards silent drift) ---


def test_enum_value_strings_match_spec() -> None:
    assert [p.value for p in Polarity] == ["asserted", "negated"]
    assert [m.value for m in Modality] == ["categorical", "probable", "possible", "hypothesized"]
    assert [a.value for a in Attribution] == ["document", "reported-speech", "named-source"]
    assert [e.value for e in EpistemicClass] == ["observation", "testimony", "judgement"]
    assert [r.value for r in Routing] == ["fact", "judgement"]
