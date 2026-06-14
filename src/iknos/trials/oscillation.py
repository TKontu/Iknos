"""Oscillation detection for cyclic QBAF regions (Trial B2; architecture.md §13, principle 8).

A cyclic argument region (mutual support, circular refutation) has **no general convergence
guarantee** under a gradual semantics (``core/qbaf.py`` docstring / §13). The architecture's
standing rule is *surface, don't force convergence*: a region that does not settle is presented
as **unresolved** with its subgraph, never smoothed into a false verdict. The engine
(``core/qbaf.solve``) already returns a ``QbafResult.unstable`` set via a **per-step** test —
"max strength change over one sweep ≤ ``tolerance``". B2 asks for a second, complementary
detector: **strength variance over the last ``n`` iterations > ``ε``**. This module is that
detector, kept pure so it lives in the LLM-free, ``DATABASE_URL``-free trials harness while
``core/qbaf.py`` stays untouched (Phase-4 oscillation handling / Phase-6 cyclic-region
presentation are the gated consumers — see ``docs/trials/b2_cyclic_oscillation.md``).

**Why a variance window, not just the engine's per-step test.** The per-step test has two
empirically-measured weaknesses (the B2 report quantifies both):

* A **slow-but-converging** trajectory (a damped oscillation whose decay rate is near 1 — e.g.
  full-strength circular refutation just under the critical base score) needs an impractically
  large iteration cap to drive its per-step change below a tight ``tolerance`` (≈ thousands of
  sweeps for ``1e-9``), so it reads as ``unstable`` at any sane bound — a *false* cycle flag.
* A steady limit cycle has a **constant** per-step change, so the per-step magnitude alone does
  not distinguish "oscillating" from "still descending".

Variance over a trailing window asks instead *is the strength still moving across the window* —
which tends to **0** once a trajectory has flattened (whatever its transient shape) and stays
**bounded away from 0** for a sustained cycle. It therefore releases a slow converger at a much
smaller budget than the strict per-step tolerance does, while still flagging a true cycle.

**Caveat, recorded honestly.** Variance over a window conflates a *sustained oscillation* with a
trajectory still *drifting monotonically* toward its fixpoint — both have non-zero variance.
The detector is valid only **past the transient**: the iteration bound (B2's other decision)
must be large enough that a genuinely-converging trajectory has flattened before the window is
read. The companion script ``scripts/b2_cyclic_oscillation.py`` sets the bound, window and ``ε``
empirically and shows the separation gap.

Pure standard library (``statistics``); operates on a **trajectory** — the per-sweep sequence of
acceptability states a gradual semantics produces — never on the engine itself.
"""

from __future__ import annotations

import statistics
from collections.abc import Hashable, Mapping, Sequence

# One sweep's output: every argument's strength after that sweep (the shape of
# ``QbafResult.acceptability``). A trajectory is the ordered sequence of these states.
State = Mapping[Hashable, float]


def _validate(trajectory: Sequence[State], window: int) -> None:
    if window < 2:
        raise ValueError(f"window must be >= 2 (a variance needs >= 2 points), got {window}")
    if len(trajectory) < window:
        raise ValueError(f"trajectory has {len(trajectory)} states, fewer than the window {window}")


def tail_variances(trajectory: Sequence[State], window: int) -> dict[Hashable, float]:
    """Population variance of each argument's strength over the **last ``window`` states**.

    Returns ``{argument: variance}`` for every argument in the windowed trajectory. Population
    variance (``statistics.pvariance``) — not sample — so a perfectly flat tail returns exactly
    ``0.0`` and the value is the literal spread of the observed strengths, not an estimator.

    The trajectory must be over a **fixed argument set** (a gradual semantics always reports
    every argument every sweep); an argument missing from any windowed state raises
    :class:`KeyError`, surfacing the malformed trajectory rather than silently dropping it.
    """
    _validate(trajectory, window)
    tail = trajectory[-window:]
    keys = list(tail[0])
    return {k: statistics.pvariance([state[k] for state in tail]) for k in keys}


def max_tail_variance(trajectory: Sequence[State], window: int) -> float:
    """The largest per-argument tail variance — the scalar fed to the threshold test.

    A region is "still moving" iff its **most** unstable argument is; taking the max (rather
    than a mean) means one persistently oscillating argument is never averaged away by its quiet
    neighbours. Returns ``0.0`` for an argument-free trajectory.
    """
    variances = tail_variances(trajectory, window)
    return max(variances.values(), default=0.0)


def oscillating_args(
    trajectory: Sequence[State], *, window: int, epsilon: float
) -> frozenset[Hashable]:
    """The arguments whose tail variance **exceeds** ``epsilon`` — the unresolved subgraph.

    This is the set Phase-6 surfaces as the "unresolved region" (§13: present the subgraph, do
    not force a verdict). The comparison is strict (``> epsilon``), so a tail variance exactly at
    the threshold counts as settled. ``epsilon`` must be non-negative.
    """
    if epsilon < 0:
        raise ValueError(f"epsilon must be non-negative, got {epsilon}")
    return frozenset(
        arg for arg, var in tail_variances(trajectory, window).items() if var > epsilon
    )


def is_oscillating(trajectory: Sequence[State], *, window: int, epsilon: float) -> bool:
    """Whether **any** argument is still moving past the transient — the B2 oscillation flag.

    ``True`` iff :func:`oscillating_args` is non-empty: at least one argument's strength varies
    by more than ``epsilon`` (variance) over the trailing ``window``. Valid only once the run is
    past the transient (see the module docstring); the iteration bound guarantees that.
    """
    return bool(oscillating_args(trajectory, window=window, epsilon=epsilon))
