"""Hand-computed fixtures for the B2 oscillation criterion (``iknos.trials.oscillation``).

Each case pins the *definition* of the variance-over-the-last-``n``-iterations detector against
a value computed by hand in the comment, and the documented edge cases (too-small window, short
trajectory, negative ``epsilon``, malformed trajectory) are asserted to raise. The criterion is
the B2 deliverable Phase-4/Phase-6 consume, so it is unit-tested exactly like the V3 metrics.
"""

from __future__ import annotations

import pytest

from iknos.trials.oscillation import (
    is_oscillating,
    max_tail_variance,
    oscillating_args,
    tail_variances,
)

# A period-2 limit cycle on one argument: {0, 1, 0, 1, ...}. Over any even window the population
# variance is 0.25 (mean 0.5, every point 0.5 away). The canonical "still oscillating" trace.
PERIOD_2 = [{"a": float(i % 2)} for i in range(20)]

# A flat (settled) tail: a transient then a constant. Tail variance is exactly 0.
SETTLED = [{"a": 0.9}, {"a": 0.7}, {"a": 0.61}] + [{"a": 0.6}] * 10


# --- tail_variances ---


def test_tail_variances_period_two_is_one_quarter() -> None:
    # last 8 of an alternating 0/1 series: mean 0.5, pvariance = mean((x-0.5)^2) = 0.25.
    assert tail_variances(PERIOD_2, window=8) == pytest.approx({"a": 0.25})


def test_tail_variances_flat_tail_is_zero() -> None:
    # the last 10 states are all 0.6 -> zero spread.
    assert tail_variances(SETTLED, window=10) == pytest.approx({"a": 0.0})


def test_tail_variances_reads_only_the_window_not_the_transient() -> None:
    # window 4 over [.., 0.61, 0.6, 0.6, 0.6] excludes the noisy head; still flat -> 0.
    assert tail_variances(SETTLED, window=3) == pytest.approx({"a": 0.0})


def test_tail_variances_per_argument_independent() -> None:
    # 'a' alternates 0/1 (var 0.25 over an even window), 'b' is constant (var 0).
    traj = [{"a": float(i % 2), "b": 0.4} for i in range(10)]
    assert tail_variances(traj, window=6) == pytest.approx({"a": 0.25, "b": 0.0})


# --- max_tail_variance ---


def test_max_tail_variance_takes_the_loudest_argument() -> None:
    traj = [{"a": float(i % 2), "b": 0.4} for i in range(10)]
    assert max_tail_variance(traj, window=6) == pytest.approx(0.25)


# --- oscillating_args / is_oscillating ---


def test_oscillating_args_selects_only_movers_above_epsilon() -> None:
    traj = [{"a": float(i % 2), "b": 0.4} for i in range(10)]
    assert oscillating_args(traj, window=6, epsilon=1e-6) == frozenset({"a"})
    assert is_oscillating(traj, window=6, epsilon=1e-6) is True


def test_settled_tail_is_not_oscillating() -> None:
    assert oscillating_args(SETTLED, window=10, epsilon=1e-6) == frozenset()
    assert is_oscillating(SETTLED, window=10, epsilon=1e-6) is False


def test_epsilon_comparison_is_strict() -> None:
    # variance exactly 0.25; epsilon exactly 0.25 -> not strictly greater -> settled.
    assert oscillating_args(PERIOD_2, window=8, epsilon=0.25) == frozenset()
    assert oscillating_args(PERIOD_2, window=8, epsilon=0.249) == frozenset({"a"})


# --- edge cases (the documented contract) ---


def test_window_below_two_raises() -> None:
    with pytest.raises(ValueError, match="window must be >= 2"):
        tail_variances(PERIOD_2, window=1)


def test_trajectory_shorter_than_window_raises() -> None:
    with pytest.raises(ValueError, match="fewer than the window"):
        tail_variances([{"a": 0.5}], window=2)


def test_negative_epsilon_raises() -> None:
    with pytest.raises(ValueError, match="epsilon must be non-negative"):
        oscillating_args(PERIOD_2, window=4, epsilon=-1e-9)


def test_malformed_trajectory_missing_argument_raises() -> None:
    # 'b' drops out of the last state -> KeyError surfaces the malformed trajectory.
    traj = [{"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}, {"a": 0.5}]
    with pytest.raises(KeyError):
        tail_variances(traj, window=3)
