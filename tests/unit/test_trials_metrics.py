"""Hand-computed fixtures for every V3 metric (``iknos.trials.metrics``).

Each metric is checked against a value computed by hand in the test (shown in the comment), so
the test pins the *definition*, not just "it runs". Edge cases (undefined inputs) are asserted
to raise or return nan, per the documented contract.
"""

from __future__ import annotations

import math

import pytest

from iknos.trials.metrics import (
    HypothesisStates,
    ReliabilityBin,
    StateFlipError,
    brier,
    cohen_kappa,
    ece,
    recall_at_budget,
    reliability_diagram,
    spearman_rho,
    state_flip_error,
)

# --- recall_at_budget ---


def test_recall_at_budget_top_k() -> None:
    # top-3 of [a,b,c,d,e] = {a,b,c}; gold {a,c,x}; hit {a,c} -> 2/3.
    assert recall_at_budget(["a", "b", "c", "d", "e"], {"a", "c", "x"}, budget=3) == pytest.approx(
        2 / 3
    )


def test_recall_at_budget_unretrieved_gold_counts_against() -> None:
    # x is never in the list, so even an unbounded budget recalls only {a,c} -> 2/3.
    assert recall_at_budget(["a", "b", "c"], {"a", "c", "x"}, budget=10) == pytest.approx(2 / 3)


def test_recall_at_budget_zero_budget_is_zero() -> None:
    assert recall_at_budget(["a", "b"], {"a"}, budget=0) == 0.0


def test_recall_at_budget_dedupes_within_window() -> None:
    # A repeated id cannot inflate recall: top-3 of [a,a,a] is the set {a}; gold {a,b} -> 1/2.
    assert recall_at_budget(["a", "a", "a"], {"a", "b"}, budget=3) == 0.5


def test_recall_at_budget_empty_gold_raises() -> None:
    with pytest.raises(ValueError):
        recall_at_budget(["a"], set(), budget=1)


def test_recall_at_budget_negative_budget_raises() -> None:
    with pytest.raises(ValueError):
        recall_at_budget(["a"], {"a"}, budget=-1)


# --- brier ---


def test_brier_hand_computed() -> None:
    # (0.9-1)^2 + (0.1-0)^2 + (0.8-1)^2 + (0.3-0)^2 = 0.01+0.01+0.04+0.09 = 0.15; /4 = 0.0375.
    assert brier([0.9, 0.1, 0.8, 0.3], [1, 0, 1, 0]) == pytest.approx(0.0375)


def test_brier_perfect_is_zero() -> None:
    assert brier([1.0, 0.0], [1, 0]) == 0.0


def test_brier_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        brier([0.5], [1, 0])  # length mismatch
    with pytest.raises(ValueError):
        brier([1.5], [1])  # prob out of range
    with pytest.raises(ValueError):
        brier([0.5], [2])  # outcome not 0/1


# --- ece + reliability_diagram ---


def test_ece_hand_computed() -> None:
    # Four singleton bins: gaps |acc-conf| = 0.1, 0.2, 0.2, 0.1; each weight 1/4 -> ECE 0.15.
    assert ece([0.1, 0.2, 0.9, 0.8], [0, 0, 1, 1], n_bins=10) == pytest.approx(0.15)


def test_ece_multi_item_bin() -> None:
    # Both 0.05 land in bin 0: conf 0.05, acc 0.5, gap 0.45; single non-empty bin -> ECE 0.45.
    assert ece([0.05, 0.05], [1, 0], n_bins=10) == pytest.approx(0.45)


def test_reliability_diagram_bins() -> None:
    bins = reliability_diagram([0.05, 0.05], [1, 0], n_bins=10)
    assert len(bins) == 10
    assert bins[0] == ReliabilityBin(
        lower=0.0, upper=0.1, count=2, mean_confidence=pytest.approx(0.05), accuracy=0.5
    )
    # Every other bin is empty with undefined confidence/accuracy.
    assert all(b.count == 0 and b.mean_confidence is None and b.accuracy is None for b in bins[1:])


def test_reliability_diagram_top_bin_includes_one() -> None:
    # p == 1.0 must land in the top bin, not index out of range.
    bins = reliability_diagram([1.0], [1], n_bins=10)
    assert bins[9].count == 1
    assert bins[9].upper == 1.0


# --- cohen_kappa ---


def test_cohen_kappa_hand_computed() -> None:
    # a=[1,1,0,0], b=[1,0,0,0]: p_o=3/4=0.75; p_e=0.5*0.25 + 0.5*0.75 = 0.5; k=0.25/0.5=0.5.
    assert cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) == pytest.approx(0.5)


def test_cohen_kappa_chance_level_is_zero() -> None:
    # a=[1,1,1], b=[1,1,0]: p_o=2/3, p_e=1*2/3 + 0*1/3 = 2/3 -> k = 0.
    assert cohen_kappa([1, 1, 1], [1, 1, 0]) == pytest.approx(0.0)


def test_cohen_kappa_degenerate_single_category() -> None:
    # Both annotators use one label for everything: p_e == 1; full agreement -> 1.0 by convention.
    assert cohen_kappa(["x", "x"], ["x", "x"]) == 1.0


def test_cohen_kappa_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        cohen_kappa([1, 1], [1])
    with pytest.raises(ValueError):
        cohen_kappa([], [])


# --- spearman_rho ---


def test_spearman_perfect_monotonic() -> None:
    assert spearman_rho([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)


def test_spearman_perfect_inverse() -> None:
    assert spearman_rho([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)


def test_spearman_with_ties() -> None:
    # x=[1,1,2] -> avg ranks [1.5,1.5,3]; y=[1,2,3] -> ranks [1,2,3]; rho = 1.5/sqrt(1.5*2).
    assert spearman_rho([1, 1, 2], [1, 2, 3]) == pytest.approx(1.5 / math.sqrt(3.0))


def test_spearman_constant_is_nan() -> None:
    assert math.isnan(spearman_rho([1, 1, 1], [1, 2, 3]))


def test_spearman_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        spearman_rho([1, 2], [1])
    with pytest.raises(ValueError):
        spearman_rho([1], [1])


# --- state_flip_error (the d10 measurement) ---


def _d10_states(
    *, h1: tuple[float, float], h2: tuple[float, float], h3: tuple[float, float]
) -> list[HypothesisStates]:
    return [
        HypothesisStates("H1", gold_before=1, gold_after=3, pred_before=h1[0], pred_after=h1[1]),
        HypothesisStates("H2", gold_before=3, gold_after=1, pred_before=h2[0], pred_after=h2[1]),
        HypothesisStates("H3", gold_before=0, gold_after=0, pred_before=h3[0], pred_after=h3[1]),
    ]


def test_state_flip_all_correct() -> None:
    # System flips H1 up, H2 down, holds H3 — exactly like gold.
    result = state_flip_error(_d10_states(h1=(1, 3), h2=(3, 1), h3=(0, 0)))
    assert result == StateFlipError(
        total=3,
        correct=3,
        held_when_should_flip=0,
        flipped_when_should_hold=0,
        wrong_direction=0,
    )
    assert result.error_rate == 0.0


def test_state_flip_each_error_kind() -> None:
    # H1 gold-up but pred-down -> wrong_direction; H2 gold-down but pred-held ->
    # held_when_should_flip; H3 gold-held but pred-up -> flipped_when_should_hold.
    result = state_flip_error(_d10_states(h1=(3, 1), h2=(1, 1), h3=(0, 2)))
    assert result == StateFlipError(
        total=3,
        correct=0,
        held_when_should_flip=1,
        flipped_when_should_hold=1,
        wrong_direction=1,
    )
    assert result.error_rate == pytest.approx(1.0)


def test_state_flip_empty_raises() -> None:
    with pytest.raises(ValueError):
        state_flip_error([])


def test_state_flip_error_rate_undefined_for_zero() -> None:
    with pytest.raises(ValueError):
        _ = StateFlipError(0, 0, 0, 0, 0).error_rate
