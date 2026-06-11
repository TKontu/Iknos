"""G3.9 — unit tests for the composed-loop termination driver (DB-free).

Pins the §12/§13 discipline: a fixpoint converges; an oscillation is detected and its cycle
surfaced; a slow/divergent loop is bounded; the loop ALWAYS terminates and never silently
re-iterates. Toy state machines stand in for the Phase-4 ``REFUTES→A→B→QBAF`` step.
"""

import pytest

from iknos.core.composed_loop import Stability, stabilize


def test_converges_to_a_fixpoint() -> None:
    # Halving toward 0 (integer): 8,4,2,1,0,0 -> fixpoint at 0.
    res = stabilize(8, lambda n: n // 2, max_iterations=100)
    assert res.status is Stability.CONVERGED
    assert res.converged
    assert res.state == 0
    assert not res.is_finding
    assert res.unstable_region() == ()
    assert res.trajectory[0] == 8 and res.trajectory[-1] == 0


def test_immediate_fixpoint_in_one_step() -> None:
    res = stabilize(5, lambda n: 5, max_iterations=10)
    assert res.status is Stability.CONVERGED
    assert res.iterations == 1


def test_detects_a_two_cycle_oscillation() -> None:
    # 0 -> 1 -> 0 -> 1 ... a period-2 oscillation, surfaced as a finding.
    res = stabilize(0, lambda n: 1 - n, max_iterations=50)
    assert res.status is Stability.OSCILLATING
    assert res.is_finding
    assert set(res.cycle) == {0, 1}
    assert res.unstable_region() == res.cycle
    # Bounded well under max_iterations — it stopped as soon as it repeated.
    assert res.iterations <= 3


def test_detects_a_longer_cycle_with_a_tail() -> None:
    # A lead-in then a 3-cycle: 9 -> 0 -> 1 -> 2 -> 0 -> 1 -> 2 ...
    nxt = {9: 0, 0: 1, 1: 2, 2: 0}
    res = stabilize(9, lambda n: nxt[n], max_iterations=50)
    assert res.status is Stability.OSCILLATING
    # The cycle is the recurring region (0,1,2), not the 9 lead-in.
    assert set(res.cycle) == {0, 1, 2}
    assert 9 not in res.cycle


def test_diverges_when_bound_is_hit() -> None:
    # Strictly increasing, never repeats, never a fixpoint -> bounded as DIVERGED.
    res = stabilize(0, lambda n: n + 1, max_iterations=5)
    assert res.status is Stability.DIVERGED
    assert res.is_finding
    assert res.iterations == 5
    assert res.state == 5
    # The whole trajectory is surfaced as the unstable region.
    assert res.unstable_region() == res.trajectory


def test_unhashable_state_uses_a_key_function() -> None:
    # State is a dict (unhashable); a 2-cycle between two dict states is detected via `key`.
    a, b = {"x": 0}, {"x": 1}

    def step(s: dict) -> dict:
        return b if s == a else a

    res = stabilize(a, step, max_iterations=20, key=lambda s: frozenset(s.items()))
    assert res.status is Stability.OSCILLATING
    assert {frozenset(s.items()) for s in res.cycle} == {
        frozenset(a.items()),
        frozenset(b.items()),
    }


def test_max_iterations_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        stabilize(0, lambda n: n, max_iterations=0)


def test_always_terminates_even_on_pathological_step() -> None:
    # A step that never converges and never repeats within the bound still returns (bounded).
    res = stabilize(0, lambda n: n + 7, max_iterations=3)
    assert res.status is Stability.DIVERGED
    assert res.iterations == 3
