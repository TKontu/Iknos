"""Intentional-layer + hypothesis-presentation vocabulary (§11.2).

The investigation's goal is a first-class **intentional layer** over the
epistemic graph, and the two layers have **different semantics that must not be
conflated** (§11.2):

- a **`Task` is *answered*** (``open → answered``) — it frames and scopes the
  investigation; it *never* becomes "true". Vocabulary: ``TaskType``,
  ``AnswerState``.
- a **`Hypothesis` is *adjudicated*** — its truth-state is **computed** from
  evidence by the QBAF (§8), never set by hand. Vocabulary: ``HypothesisState``
  (the discrete state) and ``AcceptabilityBand`` (the real-valued QBAF strength
  banded for presentation — *not* a parallel truth system, §11.2).

This module is the **property contract** for the ``Task`` and ``Hypothesis`` AGE
labels (created by migration 0004 / present since 0001). Following the Phase 0
convention, the *labels and their property vocabularies* are fixed here now; the
full Pydantic node projections land with their consumers (Task → Phase 6,
Hypothesis adjudication → Phase 4). The banding *policy* (thresholds) and the
QBAF that produces ``acceptability`` are likewise deferred — only the stable
vocabulary and the band boundaries live here.

All enums are ``StrEnum`` so they serialize to plain strings for the AGE layer
(``db/age.py:cypher_map``), exactly like ``Tier`` / ``SensitivityLevel``.
"""

from enum import StrEnum


class TaskType(StrEnum):
    """The framing-question type of a ``Task`` (§11.2).

    Open vocabulary: §11.2 lists "causal, normative, existence, comparative, …".
    The set is intentionally extensible — adding a member is an **additive,
    non-breaking** change (the value is just a string the graph stores; no
    migration), so new investigation shapes do not force a schema break.
    """

    CAUSAL = "causal"  # "why did X fail?"
    NORMATIVE = "normative"  # "was maintenance negligent?"
    EXISTENCE = "existence"  # "did Z happen?"
    COMPARATIVE = "comparative"  # "which of X/Y is more …?"


class AnswerState(StrEnum):
    """A ``Task``'s progress toward being **answered** (§11.2).

    This is *answeredness*, deliberately distinct from epistemic truth: a Task is
    never adjudicated true/false (that is ``HypothesisState``). ``abandoned`` is a
    terminal non-answer (e.g. out of scope or unanswerable within budget).
    """

    OPEN = "open"
    PARTIALLY_ANSWERED = "partially-answered"
    ANSWERED = "answered"
    ABANDONED = "abandoned"


class HypothesisState(StrEnum):
    """A ``Hypothesis``'s adjudicated state (§10, §11.2).

    **Computed, never set by hand** — derived from the SUPPORTS/REFUTES evidence
    by the QBAF (§8/§12). Distinct from ``AcceptabilityBand``: ``state`` is the
    coarse support outcome, the band is the graded presentation of the underlying
    real-valued ``acceptability``.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    REFUTED = "refuted"


class AcceptabilityBand(StrEnum):
    """Presentation banding of a ``Hypothesis.acceptability`` strength (§11.2).

    ``acceptability`` is the real-valued QBAF gradual-semantics strength in
    ``[0, 1]``; for display it bands into a graded verdict. This is **presentation
    only** — not a parallel truth system, and never a stored substitute for the
    real value (cf. credibility is derived-not-stored, §9.1/§14). Compute the band
    from the strength at render time via :func:`band`.
    """

    FALSE = "false"
    IMPLAUSIBLE = "implausible"
    PLAUSIBLE = "plausible"
    TRUE = "true"


# Single source of truth for the banding policy: the inclusive lower bound at
# which each band begins, ordered low → high over acceptability ∈ [0, 1]. These
# are **placeholder thresholds** — symmetric quarters around the 0.5 neutral
# point — to be calibrated in Phase 4/6 against QBAF output; the *only* place the
# policy is encoded, so calibration is a one-line change here. Do not duplicate
# these numbers at call sites; call :func:`band`.
_BAND_LOWER_BOUNDS: tuple[tuple[float, AcceptabilityBand], ...] = (
    (0.75, AcceptabilityBand.TRUE),
    (0.50, AcceptabilityBand.PLAUSIBLE),
    (0.25, AcceptabilityBand.IMPLAUSIBLE),
    (0.00, AcceptabilityBand.FALSE),
)


def band(acceptability: float) -> AcceptabilityBand:
    """Band a QBAF acceptability strength into a presentation verdict (§11.2).

    Boundaries are half-open ``[lower, upper)`` so each band owns its lower bound:
    ``0.50 → PLAUSIBLE``, ``0.25 → IMPLAUSIBLE``, ``1.0 → TRUE``. Raises
    ``ValueError`` for values outside ``[0, 1]`` — acceptability is defined only
    on the unit interval, so an out-of-range value is a caller bug, surfaced
    rather than silently clamped.
    """
    if not 0.0 <= acceptability <= 1.0:
        raise ValueError(f"acceptability must be in [0, 1], got {acceptability!r}")
    for lower, verdict in _BAND_LOWER_BOUNDS:
        if acceptability >= lower:
            return verdict
    # Unreachable: 0.0 is the last lower bound and the guard rejects negatives.
    raise AssertionError("band thresholds must cover [0, 1] down to 0.0")
