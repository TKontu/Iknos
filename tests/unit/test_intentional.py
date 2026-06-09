"""Unit tests for the intentional/presentation vocabulary (G0.4, G0.5).

Covers the acceptability banding boundaries, fail-fast on out-of-range input,
plain-string serialization (the AGE contract), and the INVOLVES role vocabulary.
"""

import pytest

from iknos.types.edges import Role
from iknos.types.intentional import (
    AcceptabilityBand,
    AnswerState,
    HypothesisState,
    TaskType,
    band,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, AcceptabilityBand.FALSE),
        (0.24, AcceptabilityBand.FALSE),
        (0.25, AcceptabilityBand.IMPLAUSIBLE),  # lower bound is inclusive
        (0.49, AcceptabilityBand.IMPLAUSIBLE),
        (0.50, AcceptabilityBand.PLAUSIBLE),
        (0.74, AcceptabilityBand.PLAUSIBLE),
        (0.75, AcceptabilityBand.TRUE),
        (1.0, AcceptabilityBand.TRUE),
    ],
)
def test_band_boundaries(value: float, expected: AcceptabilityBand) -> None:
    assert band(value) is expected


@pytest.mark.parametrize("bad", [-0.01, 1.01, -1.0, 2.0])
def test_band_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        band(bad)


def test_band_is_monotonic_nondecreasing() -> None:
    order = [
        AcceptabilityBand.FALSE,
        AcceptabilityBand.IMPLAUSIBLE,
        AcceptabilityBand.PLAUSIBLE,
        AcceptabilityBand.TRUE,
    ]
    rank = {b: i for i, b in enumerate(order)}
    prev = -1
    x = 0.0
    while x <= 1.0:
        r = rank[band(round(x, 2))]
        assert r >= prev
        prev = r
        x += 0.05


def test_all_bands_are_reachable() -> None:
    produced = {band(x) for x in (0.0, 0.3, 0.6, 0.9)}
    assert produced == set(AcceptabilityBand)


def test_enums_serialize_as_plain_strings() -> None:
    # The AGE layer (cypher_map) relies on StrEnum == its plain string value.
    assert str(TaskType.CAUSAL) == "causal"
    assert str(AnswerState.PARTIALLY_ANSWERED) == "partially-answered"
    assert str(HypothesisState.REFUTED) == "refuted"
    assert str(AcceptabilityBand.IMPLAUSIBLE) == "implausible"
    assert str(Role.SUBJECT) == "subject"


def test_vocabularies_match_spec() -> None:
    assert {t.value for t in TaskType} == {"causal", "normative", "existence", "comparative"}
    assert {s.value for s in AnswerState} == {
        "open",
        "partially-answered",
        "answered",
        "abandoned",
    }
    assert {s.value for s in HypothesisState} == {"supported", "unsupported", "refuted"}
    assert {b.value for b in AcceptabilityBand} == {"false", "implausible", "plausible", "true"}
    assert {r.value for r in Role} == {"subject", "object", "instrument"}
