"""Composed-loop termination — bounded iteration + oscillation detection (G3.9; §7.2, §12, §13).

The reasoning core's outer feedback loop — ``REFUTES → retract → Layer A → Layer B → QBAF →
…`` — is **not guaranteed to converge**. Evidence can be genuinely circular or balanced, and
§13 is emphatic that this is a **finding, not a blocker**: "a non-converged or oscillating
region is a *finding* (genuinely circular or balanced evidence) to be detected, bounded, and
surfaced to the investigator, not smoothed into a false verdict. Requirement is therefore:
bound the iteration, detect oscillation, and present the region as unresolved with its
subgraph — not guarantee a fixed point."

This module is that **termination discipline**, as a pure, reusable driver. :func:`stabilize`
runs a state-transition ``step`` to a fixpoint under a hard **iteration bound**, and:

* returns :data:`Stability.CONVERGED` with the fixpoint when ``step(state) == state``;
* detects **oscillation** — a return to an earlier state (a cycle of period > 1) — and
  returns :data:`Stability.OSCILLATING` with **the cycle itself** as the unstable region;
* on hitting the bound without converging or repeating, returns
  :data:`Stability.DIVERGED` with the trajectory — never silently re-iterating.

So the loop **always terminates**, and non-convergence is surfaced as structured data the
caller turns into a finding (the "unstable sub-region with its subgraph", §12), never
swallowed. This is the *outer* analogue of the bound already inside Layer B's confidence
fixpoint (``core/confidence.py::valuate``): that one is guaranteed to converge (absorptive,
ω-continuous) and the bound is a backstop; this one is *expected* to sometimes not converge,
and the bound + cycle detection are the product behaviour.

Deliberately **generic and pure** — generic over the state type ``S`` and the ``step``
function, with no DB/LLM/graph dependency, so it is unit-testable with toy state machines and
reused wherever a bounded fixpoint with oscillation-as-finding is needed (the §5.2 merge↔split
hysteresis loop names the same "surface the unstable region, don't loop on it" discipline).

Scope deliberately left to later phases (documented seams):

- **The actual composed step** — wiring ``REFUTES → retract → Layer A → Layer B → QBAF`` into
  ``step`` needs the Phase 4 evidential layer (``SUPPORTS``/``REFUTES``, the QBAF gradual
  semantics) that does not exist yet. This module supplies the *driver*; Phase 4 supplies the
  body and maps an :class:`StabilizationResult`'s unstable region to a graph finding.
- **Monotonic-in-effort re-inference** (§12, §6.1) — bounding *expensive* re-inference to at
  most once per evidence-state (the content-addressed cache key extended with the region's
  state hash) is a caching concern layered on top of this bound, not part of the driver.
"""

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from enum import StrEnum


class Stability(StrEnum):
    """How a composed loop terminated (§12, §13)."""

    CONVERGED = "converged"  # reached a fixpoint: step(state) == state
    OSCILLATING = "oscillating"  # returned to an earlier state — a cycle of period > 1
    DIVERGED = "diverged"  # hit the iteration bound without converging or repeating


@dataclass(frozen=True)
class StabilizationResult[S]:
    """The outcome of :func:`stabilize` — terminating, with non-convergence made explicit.

    ``state`` is the final state reached (the fixpoint when :data:`Stability.CONVERGED`).
    ``trajectory`` is the full sequence visited, ``initial`` first — the audit trail of the
    loop. ``cycle`` is the oscillating states (the states from the first recurrence onward),
    non-empty **only** when :data:`Stability.OSCILLATING`; it *is* the unstable sub-region to
    surface as a finding. ``iterations`` is the number of ``step`` applications performed.
    """

    status: Stability
    state: S
    iterations: int
    trajectory: tuple[S, ...]
    cycle: tuple[S, ...] = field(default_factory=tuple)

    @property
    def converged(self) -> bool:
        """Whether the loop reached a genuine fixpoint."""
        return self.status is Stability.CONVERGED

    @property
    def is_finding(self) -> bool:
        """Whether the outcome is an **unstable region to surface** (oscillating or diverged)
        rather than a resolved fixpoint (§13 — surface, don't smooth into a false verdict)."""
        return self.status is not Stability.CONVERGED

    def unstable_region(self) -> tuple[S, ...]:
        """The states to surface as the finding: the cycle if oscillating, else (for a
        diverged loop) the whole trajectory — empty when converged. The caller maps these to
        the unresolved subgraph (§12)."""
        if self.status is Stability.OSCILLATING:
            return self.cycle
        if self.status is Stability.DIVERGED:
            return self.trajectory
        return ()


def stabilize[S](
    initial: S,
    step: Callable[[S], S],
    *,
    max_iterations: int,
    key: Callable[[S], Hashable] | None = None,
) -> StabilizationResult[S]:
    """Run ``step`` from ``initial`` to a fixpoint under a hard ``max_iterations`` bound,
    detecting oscillation — the §12/§13 composed-loop termination discipline.

    ``step`` is the state transition (in the reasoning loop, one ``retract → A → B → QBAF``
    pass). ``key`` derives a **hashable identity** for cycle detection when ``S`` is not itself
    hashable (e.g. a dict state → ``frozenset(s.items())``); when omitted, the state is used
    directly and must be hashable. The loop:

    1. applies ``step``; if the result equals the current state (by ``key``) → **converged**,
       the fixpoint;
    2. else if the result's key was seen at an **earlier** step → **oscillating**; the cycle
       (earlier-occurrence … now) is returned as the unstable region;
    3. else records it and continues, up to ``max_iterations`` ``step`` applications, after
       which → **diverged**.

    Always terminates in at most ``max_iterations`` steps and never raises on non-convergence
    — the outcome is structured so the caller surfaces a finding instead of looping (§12).
    ``max_iterations`` must be ``>= 1``.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    identity: Callable[[S], Hashable] = key or (lambda s: s)

    current = initial
    trajectory: list[S] = [initial]
    # state-key -> index in `trajectory` (for locating the start of an oscillation cycle).
    seen: dict[Hashable, int] = {identity(initial): 0}

    for i in range(1, max_iterations + 1):
        nxt = step(current)
        nxt_key = identity(nxt)

        if nxt_key == identity(current):
            # Fixpoint: the desired convergence. Record the (equal) final state for the trail.
            return StabilizationResult(
                status=Stability.CONVERGED,
                state=nxt,
                iterations=i,
                trajectory=tuple(trajectory),
            )

        if nxt_key in seen:
            # Returned to an earlier (non-immediately-prior) state: a cycle of period > 1.
            cycle = tuple(trajectory[seen[nxt_key] :])
            return StabilizationResult(
                status=Stability.OSCILLATING,
                state=nxt,
                iterations=i,
                trajectory=tuple((*trajectory, nxt)),
                cycle=cycle,
            )

        seen[nxt_key] = len(trajectory)
        trajectory.append(nxt)
        current = nxt

    # Bound reached without a fixpoint or a detected cycle — surfaced, never re-iterated.
    return StabilizationResult(
        status=Stability.DIVERGED,
        state=current,
        iterations=max_iterations,
        trajectory=tuple(trajectory),
    )
