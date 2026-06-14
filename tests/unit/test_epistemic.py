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
    Stakes,
    calibrate_agreement,
    combine_faithfulness,
    decode_provisional_reasons,
    faithfulness_from_verdict,
    legacy_provisional,
    merge_provisional_reasons,
    provisional_reasons_for,
    provisional_threshold_for,
    reassess_faithfulness_reasons,
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


# --- provisional reasons: the faithfulness leg (R8; replaces the is_provisional bool gate) ---


def test_provisional_reasons_half_open_boundary() -> None:
    assert provisional_reasons_for(0.49) == {_LOW}
    assert provisional_reasons_for(0.5) == set()  # at-threshold NOT provisional (band() convention)
    assert provisional_reasons_for(0.9) == set()


def test_provisional_reasons_custom_threshold() -> None:
    assert provisional_reasons_for(0.7, threshold=0.8) == {_LOW}
    assert provisional_reasons_for(0.8, threshold=0.8) == set()


# --- the stakes-dependent threshold (G1.6, §3.1: "the threshold is stakes-dependent") ---


@pytest.mark.parametrize("stakes", list(Stakes))
def test_provisional_threshold_covers_every_stakes(stakes: Stakes) -> None:
    # No KeyError for any member → the threshold map is exhaustive (fail-loud on a future
    # calibrated band, the same discipline as route_for / the entailment base).
    assert isinstance(provisional_threshold_for(stakes), float)


def test_high_stakes_bar_is_stricter_than_low() -> None:
    # §3.1: "a reference feeding a high-significance refutation needs higher confidence than one
    # feeding a minor corroboration." The bar a move's grounding must clear rises with its stakes.
    assert provisional_threshold_for(Stakes.HIGH) > provisional_threshold_for(Stakes.LOW)


def test_low_stakes_bar_is_permissive_zero() -> None:
    # LOW sets no faithfulness bar (0.0) — a provisional atom may exist and corroborate; this is
    # the value the quarantine gate reads as "LOW always passes" (§3.1, R9).
    assert provisional_threshold_for(Stakes.LOW) == 0.0


def test_default_provisional_floor_is_the_high_stakes_bar() -> None:
    # The default threshold of provisional_reasons_for IS the high-stakes bar: an atom is marked
    # provisional precisely when it would be quarantined from the strictest gated move. Single
    # source of truth — the 0.5 boundary the operator penalties are tuned against lives only in the
    # stakes map now.
    high = provisional_threshold_for(Stakes.HIGH)
    assert provisional_reasons_for(high) == set()  # at-bar passes (half-open)
    assert provisional_reasons_for(high - 0.01) == {_LOW}  # just below is provisional


def test_provisional_reasons_none_is_unassessed() -> None:
    # G1.21 (§3.1 D2 behavior change): the verifier-off mode computes no faithfulness, and
    # unassessed grounding is provisional — never coerced toward trusted. (Was `== set()` pre-G1.21;
    # repinned deliberately.)
    assert provisional_reasons_for(None) == {ProvisionalReason.UNASSESSED_FAITHFULNESS}


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_provisional_reasons_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        provisional_reasons_for(bad)


# --- the reason vocabulary, OR-fold, decode, and the legacy-boolean transition helper (R8) ---


def test_provisional_reason_values_match_spec() -> None:
    assert [r.value for r in ProvisionalReason] == [
        "low_faithfulness",
        "unassessed_faithfulness",
        "polarity_unstable",
        "unresolved_reference",
        "uninferred_budget",
    ]


def test_merge_provisional_reasons_unions_dedupes_and_sorts() -> None:
    merged = merge_provisional_reasons(
        ["low_faithfulness"],
        [ProvisionalReason.UNRESOLVED_REFERENCE, ProvisionalReason.LOW_FAITHFULNESS],
    )
    assert merged == ["low_faithfulness", "unresolved_reference"]  # deduped, sorted, list[str]


def test_merge_provisional_reasons_empty_is_empty() -> None:
    assert merge_provisional_reasons([], []) == []


# --- reassessing the faithfulness leg on verify-backfill (G1.22): replace, don't accumulate ---


def test_reassess_replaces_unassessed_with_assessed_result() -> None:
    # The verify-backfill case: a degraded atom carried UNASSESSED; once verified faithful, the
    # faithfulness leg is *cleared*, not OR-folded into "both" (UNASSESSED must not survive).
    assert reassess_faithfulness_reasons(["unassessed_faithfulness"], 0.9) == []
    # Verified low → UNASSESSED replaced by LOW_FAITHFULNESS.
    assert reassess_faithfulness_reasons(["unassessed_faithfulness"], 0.2) == ["low_faithfulness"]


def test_reassess_preserves_non_faithfulness_reasons() -> None:
    # A G1.14 twin's POLARITY_UNSTABLE is an independent axis — it survives re-assessment, while
    # the faithfulness leg flips from UNASSESSED to the verified result.
    assert reassess_faithfulness_reasons(["polarity_unstable", "unassessed_faithfulness"], 0.9) == [
        "polarity_unstable"
    ]
    assert reassess_faithfulness_reasons(["polarity_unstable", "low_faithfulness"], 0.2) == [
        "low_faithfulness",
        "polarity_unstable",
    ]


def test_reassess_null_faithfulness_is_unassessed_again() -> None:
    # Idempotent on the degraded direction: re-assessing with a still-null score keeps UNASSESSED.
    assert reassess_faithfulness_reasons([], None) == ["unassessed_faithfulness"]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, []),
        ('["low_faithfulness"]', ["low_faithfulness"]),  # JSON string (AGE read-back)
        (["polarity_unstable"], ["polarity_unstable"]),  # real list (pure round-trip)
    ],
)
def test_decode_provisional_reasons_accepts_all_shapes(stored: object, expected: list[str]) -> None:
    assert decode_provisional_reasons(stored) == expected


def test_legacy_provisional_reproduces_the_tristate() -> None:
    assert legacy_provisional(None, []) is None  # nothing determined (verifier off, no reason)
    assert legacy_provisional(0.6, []) is False  # verified clean
    assert legacy_provisional(0.4, ["low_faithfulness"]) is True  # verified provisional
    assert legacy_provisional(None, ["polarity_unstable"]) is True  # twin in verifier-off mode


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
    # Every verdict yields a value in [0, 1] that provisional_reasons_for() accepts.
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
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        combine_faithfulness(1.0, 1.0, bad)


def test_combine_parse_quality_defaults_to_identity() -> None:
    # The third factor defaults to 1.0, so every existing two-arg call is unchanged (G1.0r).
    assert combine_faithfulness(0.8, 1.0) == pytest.approx(combine_faithfulness(0.8, 1.0, 1.0))


def test_combine_parse_quality_is_a_third_multiplicative_factor() -> None:
    # A scanned source discounts a fully-verified, stable proposition (G1.0/§3.1).
    assert combine_faithfulness(1.0, 1.0, 0.6) == pytest.approx(0.6)
    assert combine_faithfulness(1.0, 0.5, 0.6) == pytest.approx(0.3)


def test_combine_bad_parse_quality_cannot_be_rescued() -> None:
    # A verified-but-badly-parsed atom is pulled below the provisional threshold despite the
    # verifier passing it — parse quality is an independent defect (cf. agreement instability).
    score = combine_faithfulness(1.0, 1.0, 0.4)
    assert provisional_reasons_for(score) == {_LOW}


# --- agreement calibration seam (G1.20): identity now, per-model curve at Trial A3 ---


def test_calibrate_agreement_is_identity() -> None:
    # Ships identity until Trial A3 fits the per-model curve — every raw value maps to itself,
    # so combine_faithfulness is byte-identical to pre-G1.20.
    for a in (0.0, 1 / 3, 2 / 3, 1.0):
        assert calibrate_agreement(a) == pytest.approx(a)


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_calibrate_agreement_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        calibrate_agreement(bad)


def test_combine_default_curve_is_byte_identical_to_raw_product() -> None:
    # The default agreement_curve (identity) leaves combine exactly at the raw product — the
    # seam adds no behavior change until a curve is fitted.
    for verify, agreement, pq in [(1.0, 1 / 3, 1.0), (0.7, 2 / 3, 0.6), (1.0, 1.0, 1.0)]:
        assert combine_faithfulness(verify, agreement, pq) == pytest.approx(verify * agreement * pq)


def test_combine_non_identity_curve_moves_faithfulness_conservatively() -> None:
    # A fitted (conservative) curve can only pull faithfulness *down* vs. the raw product. A
    # square map is concave-down on [0, 1] (x² ≤ x), a stand-in for the Wilson-style shrink.
    conservative = combine_faithfulness(1.0, 0.5, agreement_curve=lambda a: a * a)
    assert conservative == pytest.approx(0.25)  # 1.0 × 0.5² × 1.0
    assert conservative < combine_faithfulness(1.0, 0.5)  # strictly below the identity path
    # The curve is applied at combine time only — it does not mutate the agreement it was handed.
    raw = 0.5
    combine_faithfulness(1.0, raw, agreement_curve=lambda a: a * a)
    assert raw == 0.5


def test_combine_rejects_a_curve_that_escapes_unit_interval() -> None:
    # A misfit curve that returns out-of-range is a bug surfaced loud, never silently clamped.
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        combine_faithfulness(1.0, 0.5, agreement_curve=lambda a: a + 1.0)


# --- enum value strings are exactly the spec strings (guards silent drift) ---


def test_enum_value_strings_match_spec() -> None:
    assert [p.value for p in Polarity] == ["asserted", "negated"]
    assert [m.value for m in Modality] == ["categorical", "probable", "possible", "hypothesized"]
    assert [a.value for a in Attribution] == ["document", "reported-speech", "named-source"]
    assert [e.value for e in EpistemicClass] == ["observation", "testimony", "judgement"]
    assert [r.value for r in Routing] == ["fact", "judgement"]
    assert [e.value for e in Entailment] == ["entailed", "neutral", "contradicted"]
