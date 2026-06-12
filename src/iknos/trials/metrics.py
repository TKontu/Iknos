"""Pure metric functions for the validation-gate trials (Trial A0 / V3).

Every function here is a pure function of its arguments — no I/O, no model, no global state —
so each is unit-tested against a hand-computed fixture (``tests/unit/test_trials_metrics.py``).
The metrics map directly onto the trials that consume them (``docs/todo_trials.md``):

* :func:`recall_at_budget` — candidate-generation recall at a budget, used **split by sign**
  (supporter vs **refuter** recall; refuter recall is the A1 binding constraint).
* :func:`brier`, :func:`ece`, :func:`reliability_diagram` — confidence calibration (A3, E1).
* :func:`cohen_kappa` — inter-annotator / attachment agreement (V2 label gate, A4).
* :func:`spearman_rho` — depth-recovery rank correlation (A4).
* :func:`state_flip_error` — the d10 hypothesis-state-flip measurement (§8): per hypothesis,
  did the state flip when it should have, hold when it should have, and in the right direction.

Design choices that keep the contract honest:

* **Undefined is loud, not silently zero.** Recall of an empty gold set, a correlation of a
  constant vector, and a κ with a degenerate chance term are mathematically undefined; the
  functions raise :class:`ValueError` or return ``float('nan')`` (documented per function)
  rather than fabricating a 0 or 1 that a report would read as a real score.
* **Outcomes are 0/1 ground truth, predictions are probabilities in [0, 1].** Inputs are
  validated; a probability outside [0, 1] or mismatched lengths raise immediately.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Hashable, Sequence
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────────────────────────────────
# Candidate-generation recall (A1) — split by sign at the call site (pass refuter gold only).
# ─────────────────────────────────────────────────────────────────────────────────────────


def recall_at_budget(
    retrieved: Sequence[Hashable],
    gold: Collection[Hashable],
    budget: int,
) -> float:
    """Fraction of ``gold`` items found within the top-``budget`` of ranked ``retrieved``.

    ``retrieved`` is the candidate list **in rank order** (most relevant first); only its
    first ``budget`` entries count as recalled. This is recall@budget: the fraction of the
    planted edges a candidate funnel surfaces within a cost ceiling. For the A1 refuter-recall
    measurement, call it twice — once with the supporter gold set, once with the refuter gold
    set — against the same ``retrieved`` list; refuter recall is the binding constraint.

    Raises ``ValueError`` if ``gold`` is empty (recall of nothing is undefined — do not let a
    report read it as 1.0) or ``budget`` is negative. Duplicate ``retrieved`` entries are
    de-duplicated within the budget window so a list that repeats one id cannot inflate recall.
    """
    if budget < 0:
        raise ValueError(f"budget must be non-negative, got {budget}")
    gold_set = set(gold)
    if not gold_set:
        raise ValueError("recall is undefined for an empty gold set")
    considered = set(retrieved[:budget])
    return len(considered & gold_set) / len(gold_set)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Calibration (A3, E1): Brier, ECE, and the reliability-diagram bins ECE is computed from.
# ─────────────────────────────────────────────────────────────────────────────────────────


def _validate_calibration_inputs(predictions: Sequence[float], outcomes: Sequence[int]) -> None:
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions and outcomes length mismatch: {len(predictions)} vs {len(outcomes)}"
        )
    if not predictions:
        raise ValueError("calibration metrics need at least one (prediction, outcome) pair")
    for p in predictions:
        if not (0.0 <= p <= 1.0):
            raise ValueError(f"prediction {p} is outside [0, 1]")
    for o in outcomes:
        if o not in (0, 1, True, False):
            raise ValueError(f"outcome {o!r} is not 0 or 1")


def brier(predictions: Sequence[float], outcomes: Sequence[int]) -> float:
    """Brier score — mean squared error of probabilistic predictions against 0/1 outcomes.

    ``mean((p_i - o_i) ** 2)``. Lower is better; 0 is perfect. A proper scoring rule, so it
    rewards calibration *and* sharpness together — reported alongside :func:`ece`, which
    isolates calibration.
    """
    _validate_calibration_inputs(predictions, outcomes)
    return sum((p - int(o)) ** 2 for p, o in zip(predictions, outcomes, strict=True)) / len(
        predictions
    )


@dataclass(frozen=True)
class ReliabilityBin:
    """One equal-width confidence bin of a reliability diagram (no plotting — just the data).

    ``lower``/``upper`` are the bin's confidence interval ``[lower, upper)`` (the top bin is
    closed at 1.0). ``count`` is how many predictions fell in it; ``mean_confidence`` and
    ``accuracy`` are ``None`` for an empty bin (a mean over nothing is undefined). The gap
    ``|accuracy - mean_confidence|``, weighted by ``count``, is the bin's contribution to ECE.
    """

    lower: float
    upper: float
    count: int
    mean_confidence: float | None
    accuracy: float | None


def reliability_diagram(
    predictions: Sequence[float],
    outcomes: Sequence[int],
    n_bins: int = 10,
) -> list[ReliabilityBin]:
    """Bin ``(prediction, outcome)`` pairs into ``n_bins`` equal-width confidence bins.

    Returns one :class:`ReliabilityBin` per bin (including empty bins, so the full [0, 1] axis
    is represented). The data a reliability diagram plots — confidence vs accuracy vs n — with
    **no plotting dependency** (architecture.md: build, not buy; the trials need the numbers,
    not a chart). :func:`ece` is computed from exactly these bins so the two never disagree.
    """
    _validate_calibration_inputs(predictions, outcomes)
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")
    width = 1.0 / n_bins
    sums = [0.0] * n_bins
    hits = [0] * n_bins
    counts = [0] * n_bins
    for p, o in zip(predictions, outcomes, strict=True):
        # Assign p to a bin; p == 1.0 belongs in the top bin (else it would index out of range).
        idx = min(int(p / width), n_bins - 1)
        sums[idx] += p
        hits[idx] += int(o)
        counts[idx] += 1
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        n = counts[i]
        bins.append(
            ReliabilityBin(
                lower=i * width,
                upper=(i + 1) * width if i < n_bins - 1 else 1.0,
                count=n,
                mean_confidence=(sums[i] / n) if n else None,
                accuracy=(hits[i] / n) if n else None,
            )
        )
    return bins


def ece(predictions: Sequence[float], outcomes: Sequence[int], n_bins: int = 10) -> float:
    """Expected Calibration Error — the count-weighted mean gap between confidence and accuracy.

    ``sum_b (n_b / N) * |accuracy_b - confidence_b|`` over the equal-width bins of
    :func:`reliability_diagram`. 0 means every confidence level matches its empirical accuracy.
    Isolates *calibration* (unlike :func:`brier`, which also rewards sharpness); the gate
    measures whether consistency-based confidence is better calibrated than verbalized.
    """
    total = len(predictions)
    bins = reliability_diagram(predictions, outcomes, n_bins)
    gap = 0.0
    for b in bins:
        if b.count and b.accuracy is not None and b.mean_confidence is not None:
            gap += (b.count / total) * abs(b.accuracy - b.mean_confidence)
    return gap


# ─────────────────────────────────────────────────────────────────────────────────────────
# Agreement (V2 label gate, A4): Cohen's κ for categorical labels from two annotators.
# ─────────────────────────────────────────────────────────────────────────────────────────


def cohen_kappa(labels_a: Sequence[Hashable], labels_b: Sequence[Hashable]) -> float:
    """Cohen's κ — chance-corrected agreement between two annotators on categorical labels.

    ``(p_o - p_e) / (1 - p_e)``, where ``p_o`` is the observed agreement rate and ``p_e`` is
    the agreement expected from the annotators' marginal label frequencies. The V2 label gate
    requires κ > 0.6 per dual-annotated family before a label family is trusted (§13); A4
    reuses it for fact→level attachment agreement.

    Degenerate case: when both annotators use a single label for everything, ``p_e == 1`` and κ
    is undefined (0/0). By convention this returns ``1.0`` when they fully agree (``p_o == 1``)
    and ``0.0`` otherwise — a single-category annotator carries no discriminating information.
    """
    if len(labels_a) != len(labels_b):
        raise ValueError(f"label length mismatch: {len(labels_a)} vs {len(labels_b)}")
    n = len(labels_a)
    if n == 0:
        raise ValueError("cohen_kappa needs at least one labelled item")
    agree = sum(1 for a, b in zip(labels_a, labels_b, strict=True) if a == b)
    p_o = agree / n
    categories = set(labels_a) | set(labels_b)
    p_e = 0.0
    for c in categories:
        pa = sum(1 for a in labels_a if a == c) / n
        pb = sum(1 for b in labels_b if b == c) / n
        p_e += pa * pb
    if math.isclose(p_e, 1.0):
        return 1.0 if math.isclose(p_o, 1.0) else 0.0
    return (p_o - p_e) / (1.0 - p_e)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Rank correlation (A4 depth recovery): Spearman ρ with average-rank tie handling.
# ─────────────────────────────────────────────────────────────────────────────────────────


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Rank ``values`` ascending, assigning tied values the average of their rank positions."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        # Positions i..j (0-based) are tied; average rank is the mean of (pos+1).
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_rho(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman's ρ — Pearson correlation of the average ranks of ``x`` and ``y``.

    Measures monotonic association, which is what A4's depth-recovery check needs (does the
    inferred abstraction level *order* the entities like the gold partonomy depth?), not linear
    fit. Ties get average ranks, so the tie-heavy depth labels are handled correctly.

    Returns ``float('nan')`` when either variable is constant (zero rank variance) — correlation
    with a constant is undefined, and a fabricated 0 would read as "no association" rather than
    "not measurable". Raises ``ValueError`` on length mismatch or fewer than two points.
    """
    if len(x) != len(y):
        raise ValueError(f"x and y length mismatch: {len(x)} vs {len(y)}")
    if len(x) < 2:
        raise ValueError("spearman_rho needs at least two points")
    rx = _average_ranks(x)
    ry = _average_ranks(y)
    n = len(rx)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    cov = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry, strict=True))
    var_x = sum((a - mean_rx) ** 2 for a in rx)
    var_y = sum((b - mean_ry) ** 2 for b in ry)
    if var_x == 0.0 or var_y == 0.0:
        return float("nan")
    return cov / math.sqrt(var_x * var_y)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Hypothesis-state flip (§8, the d10 measurement): did each hypothesis flip correctly?
# ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HypothesisStates:
    """A hypothesis's gold and predicted acceptability **before and after** the overturning fact.

    States are ordinal scalars (the caller maps an acceptability band — false < implausible <
    plausible < true — to a number; any monotonic encoding works, only the *direction* of
    change is read). The d10 event is the boundary: ``*_before`` is the state on the evidence
    excluding the overturning fact, ``*_after`` is the state once it lands.
    """

    hypothesis_id: str
    gold_before: float
    gold_after: float
    pred_before: float
    pred_after: float


@dataclass(frozen=True)
class StateFlipError:
    """The §8 hypothesis-state-flip tally over a set of hypotheses across the overturning event.

    Each hypothesis lands in exactly one bucket:

    * ``held_when_should_flip`` — gold changed state, the system did not (a missed retraction;
      the dangerous one — the differentiator that must work).
    * ``flipped_when_should_hold`` — gold held, the system changed it (an unwarranted flip).
    * ``wrong_direction`` — both changed, but the system moved the opposite way to gold.
    * ``correct`` — both held, or both flipped the same direction.

    ``error_rate`` is the fraction of hypotheses in any error bucket.
    """

    total: int
    correct: int
    held_when_should_flip: int
    flipped_when_should_hold: int
    wrong_direction: int

    @property
    def error_rate(self) -> float:
        if self.total == 0:
            raise ValueError("error_rate is undefined for zero hypotheses")
        return (
            self.held_when_should_flip + self.flipped_when_should_hold + self.wrong_direction
        ) / self.total


def _direction(before: float, after: float) -> int:
    """Sign of the state change: -1 (down), 0 (held), +1 (up)."""
    if after > before:
        return 1
    if after < before:
        return -1
    return 0


def state_flip_error(states: Sequence[HypothesisStates]) -> StateFlipError:
    """Classify each hypothesis's predicted state change against gold across the overturning fact.

    This is the d10 retraction measurement (architecture.md §8): when the overturning fact
    lands, H2 (installation error) should flip down and H1 (lubrication) should flip up, while
    the already-refuted H3/H4 should hold. A system that handles well-founded retraction flips
    the right hypotheses in the right direction; one that does not leaves them stuck.
    """
    if not states:
        raise ValueError("state_flip_error needs at least one hypothesis")
    correct = held = flipped = wrong = 0
    for s in states:
        gold_dir = _direction(s.gold_before, s.gold_after)
        pred_dir = _direction(s.pred_before, s.pred_after)
        gold_flips = gold_dir != 0
        pred_flips = pred_dir != 0
        if gold_flips and not pred_flips:
            held += 1
        elif not gold_flips and pred_flips:
            flipped += 1
        elif gold_flips and pred_flips and gold_dir != pred_dir:
            wrong += 1
        else:
            correct += 1
    return StateFlipError(
        total=len(states),
        correct=correct,
        held_when_should_flip=held,
        flipped_when_should_hold=flipped,
        wrong_direction=wrong,
    )
