"""Subjective-logic confidence-scoring core (Phase 4, G4.3 slice 1; architecture §8(c)).

The pure, in-memory algebra behind steps 3–4 of the §8 *confidence pipeline* — the in-house
re-implementation of subjective-logic operators (QBAF-Py / Uncertainpy / Jøsang's library are
**reference only**; §8 Tooling). It is to the edge-judgment pipeline what ``core/qbaf.py`` is to
adjudication and ``core/confidence.py`` is to Layer B: no DB, no AGE, no LLM, no migration — a
value algebra unit-testable with hand-built opinions.

**What §8 asks of this layer.** "Do not feed raw verbalized LLM confidence as edge weight."
Instead: (1) elicit by **multi-sample consistency**, not single-shot verbalization;
(2) **recalibrate per model**; (3) encode each judgment as a **subjective-logic opinion with
source-reliability discounting**; (4) **fuse** correlated/conflicting evidence with *cumulative
or averaging* fusion — **never raw Dempster's rule** under conflict (it misbehaves — the Zadeh
counterexample, §8(c)). This module supplies the encoding (1, the consistency→opinion map),
discounting (3), and fusion (4). The read-off — the fused, discounted opinion's **projected
probability** — is the calibrated edge ``strength`` ∈ [0, 1] the QBAF consumes (``core/qbaf.py``
already names this as the upstream that "has already decorrelated the evidence").

**The fusion decision (the G3.5/G4.1-style fixture, made before the engine is trusted).** §8
names *two* fusion operators and they are not interchangeable, so — exactly as the gradual
semantics (DF-QuAD vs Quadratic Energy) was decided in G4.1 — the choice is made with a numeric
fixture:

- **Cumulative fusion** (aleatory) assumes the sources are **independent** and *accumulates*
  certainty: each opinion shrinks the fused uncertainty further.
- **Averaging fusion** (epistemic) assumes the sources may be **dependent/correlated** and does
  **not** accumulate certainty — it is *idempotent* (N copies of one opinion fuse back to that
  opinion).
- **Decision: ``DEFAULT_FUSION = AVERAGING``.** The standing §13 risk is that **correlated LLM
  error is not removed by the edge-judgment disciplines** — several blind, randomized, multi-
  sample judgments from the same (or similar) model are *not* independent. Cumulative fusion
  over correlated judges manufactures false certainty (the fixture in
  ``test_subjective_logic.py`` shows three correlated copies collapsing the uncertainty);
  averaging cannot, because it is idempotent under correlation. This parallels the Layer B
  (Gödel over Viterbi) and QBAF (DF-QuAD over Quadratic Energy) choices: **default to the
  operator that cannot inflate; retain the other at the seam** for a genuinely decorrelated
  sub-domain (varied-model judges). The choice stays reversible — it is a value, not a branch.

**Deferred (documented seams, not regressions) — the rest of G4.3:**

- **The LLM judge (G4.3 slice — sign-before-magnitude, blind + randomized).** §8: classify the
  *sign* (supports / refutes / irrelevant) first and separately, then elicit *magnitude* only
  for non-irrelevant edges, by **ranking** competing evidence (relative, not absolute), judged
  *blind to the current hypothesis state* with *randomized* evidence order. That prompted
  elicitation produces the per-sample counts this module's :func:`opinion_from_evidence`
  consumes; sign is structural (the ``SUPPORTS`` vs ``REFUTES`` edge type, §10), decided
  upstream and categorical, so this module scores **magnitude** for a sign already fixed.
- **Per-model recalibration (step 2).** Mapping a model's raw consistency to a calibrated one
  is a *fitted* per-model curve with no data yet; like the ``combine_faithfulness`` calibration
  seam (epistemic.py) and the G4.1 verdict bands, it swaps in at :func:`opinion_from_evidence`
  (scaling the evidence) or post-projection without a contract change. Identity until G4.6
  fits it against the planted corpus.
- **The AGE producer (G4.3 slice).** Writing the ``SUPPORTS`` / ``REFUTES`` edge carrying the
  fused ``strength`` + ``significance`` (from the node/tier, §9) is the data-bound increment
  that consumes this read-off — the Phase-4 analogue of how ``derivation_adapter`` (G3.4)
  consumes ``core/confidence.py``.

All masses are on the unit interval and an out-of-range input *raises* rather than being
silently clamped — the ``epistemic.combine_faithfulness`` / ``credibility`` convention: an
out-of-range value is a caller bug, surfaced.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Opinions over the same proposition share a base rate, so equality / fusion compares them with
# this tolerance (floating-point fusion arithmetic does not land on exact decimals).
_TOL: float = 1e-9


@dataclass(frozen=True)
class Opinion:
    """A binomial subjective-logic opinion over a proposition X (Jøsang) — the unit of judgment.

    Four components, the canonical SL decomposition of evidence about a binary proposition:

    - ``belief`` — mass committed to X being true (here: this evidence *does* bear on the
      hypothesis, for the already-fixed sign).
    - ``disbelief`` — mass committed to X being false (the evidence does *not* bear).
    - ``uncertainty`` — mass committed to *neither* — the share of judgment the samples left
      open (ignorance). This is what subjective logic adds over a bare probability, and what
      lets correlated evidence be kept honest (it does not vanish under averaging fusion).
    - ``base_rate`` — the prior probability of X absent evidence; the projected probability
      interpolates the base rate across the uncertainty mass.

    Invariant ``belief + disbelief + uncertainty == 1`` (an opinion is a distribution over
    {true, false, uncommitted}); each component and the base rate lie in [0, 1]. Frozen +
    validated at construction, so every Opinion in the pipeline is a *valid* opinion and fusion
    arithmetic can assume it.
    """

    belief: float
    disbelief: float
    uncertainty: float
    base_rate: float = 0.5

    def __post_init__(self) -> None:
        for name, value in (
            ("belief", self.belief),
            ("disbelief", self.disbelief),
            ("uncertainty", self.uncertainty),
            ("base_rate", self.base_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value!r}")
        if abs(self.belief + self.disbelief + self.uncertainty - 1.0) > _TOL:
            raise ValueError(
                "belief + disbelief + uncertainty must sum to 1, got "
                f"{self.belief} + {self.disbelief} + {self.uncertainty}"
            )

    @property
    def projected_probability(self) -> float:
        """P(X) = belief + base_rate · uncertainty — the expected probability of X (§8(c)).

        Distributes the uncommitted (uncertainty) mass over true/false by the base rate: a
        vacuous opinion projects to the base rate, a dogmatic one (uncertainty 0) to its belief.
        **This is the calibrated edge ``strength``** the QBAF consumes once the opinion is fused
        and discounted — the single number that replaces the raw LLM confidence (§8, §10).
        """
        return self.belief + self.base_rate * self.uncertainty


def vacuous(*, base_rate: float = 0.5) -> Opinion:
    """The vacuous opinion — total uncertainty, no belief or disbelief (Jøsang).

    The "no evidence" opinion: it projects to the base rate. It is the **neutral element of
    cumulative fusion** (an independent non-judging source adds nothing); under **averaging
    fusion** it instead *dilutes toward uncertainty* — because averaging weights each opinion by
    its uncertainty mass, an abstaining (or fully source-discounted) judge *raises* the fused
    uncertainty rather than being ignored. That asymmetry is part of why averaging is the
    conservative default: it cannot silently drop an abstention.
    """
    return Opinion(belief=0.0, disbelief=0.0, uncertainty=1.0, base_rate=base_rate)


def opinion_from_evidence(
    positive: float,
    negative: float,
    *,
    base_rate: float = 0.5,
    prior_weight: float = 2.0,
) -> Opinion:
    """Encode multi-sample evidence as an opinion (step 1's encoding; §8(c) Beta/binomial map).

    The bridge from **multi-sample consistency** to a subjective-logic opinion: of N blind,
    randomized samples, ``positive`` judged the connection present and ``negative`` judged it
    absent (so ``positive + negative ≤ N``; samples that abstained are neither). The standard SL
    evidence-to-opinion mapping with a non-informative prior of weight ``W = prior_weight``::

        belief = positive / (positive + negative + W)
        disbelief = negative / (positive + negative + W)
        uncertainty = W / (positive + negative + W)

    So agreement raises belief and **more samples shrink uncertainty** (consistency *is*
    certainty — §3.1's discipline, here at the edge layer), while no observations leave a
    vacuous opinion (all mass on the prior → projects to the base rate). ``W = 2`` with
    ``base_rate = 1/2`` is the canonical uniform-prior choice (Jøsang); both are tunable and
    are the **per-model recalibration seam** (step 2) — a fitted curve scales the evidence here
    without changing the contract.

    Raises on negative counts or a non-positive prior weight — caller bugs, surfaced.
    """
    if positive < 0 or negative < 0:
        raise ValueError(f"evidence counts must be non-negative, got {positive!r}, {negative!r}")
    if prior_weight <= 0.0:
        raise ValueError(f"prior_weight must be > 0, got {prior_weight!r}")
    total = positive + negative + prior_weight
    return Opinion(
        belief=positive / total,
        disbelief=negative / total,
        uncertainty=prior_weight / total,
        base_rate=base_rate,
    )


def discount(opinion: Opinion, reliability: float) -> Opinion:
    """Discount an opinion by its source's reliability ∈ [0, 1] (step 3; SL trust transitivity).

    The §8 ↔ §9.1 seam: a judgment is only as trustworthy as the source it rests on, so before
    fusion each opinion is **discounted toward uncertainty** by the source's effective
    credibility (``core/credibility.py``'s ``effective_credibility``). Jøsang's scalar trust
    discounting moves committed mass into the uncommitted (uncertainty) mass::

        belief' = reliability · belief
        disbelief' = reliability · disbelief
        uncertainty' = 1 − reliability · (belief + disbelief)

    A fully reliable source (``reliability = 1``) is the identity; a wholly unreliable one
    (``reliability = 0``) yields the vacuous opinion — which under the default *averaging* fusion
    *raises* the fused uncertainty (an untrusted source dilutes rather than asserts) rather than
    contributing nothing. The base rate is unchanged — reliability discounts *evidence*, not the
    prior. Raises for an out-of-range reliability (the ``[0, 1]`` convention).
    """
    if not 0.0 <= reliability <= 1.0:
        raise ValueError(f"reliability must be in [0, 1], got {reliability!r}")
    return Opinion(
        belief=reliability * opinion.belief,
        disbelief=reliability * opinion.disbelief,
        uncertainty=1.0 - reliability * (opinion.belief + opinion.disbelief),
        base_rate=opinion.base_rate,
    )


def _require_shared_base_rate(a: Opinion, b: Opinion) -> float:
    """Fusion combines judgments of the *same* proposition, so the base rate is shared; a
    mismatch means the caller is fusing opinions about *different* propositions — a bug,
    surfaced rather than silently averaged into a meaningless prior."""
    if abs(a.base_rate - b.base_rate) > _TOL:
        raise ValueError(
            f"cannot fuse opinions with different base_rate ({a.base_rate} vs {b.base_rate}) — "
            "fusion is over judgments of the same proposition"
        )
    return a.base_rate


def cumulative_fuse(a: Opinion, b: Opinion) -> Opinion:
    """Cumulative (aleatory) belief fusion of two **independent** opinions (Jøsang).

    Accumulates evidence as if the two sources are independent, so the fused uncertainty is
    *lower* than either input's — certainty grows with each independent judgment. Use when the
    judges are genuinely decorrelated (e.g. varied models); the default is :data:`AVERAGING`
    because LLM samples usually are *not* independent (§13). Formula (non-dogmatic case)::

        κ = uA + uB − uA·uB
        belief = (bA·uB + bB·uA) / κ ;  uncertainty = (uA·uB) / κ

    When **both** inputs are dogmatic (``uncertainty == 0``) there is no uncertainty mass to
    weight by; this falls back to the equal-weight average of the beliefs — the documented limit
    of the general weighted form with equal relative weights.
    """
    base_rate = _require_shared_base_rate(a, b)
    if a.uncertainty <= _TOL and b.uncertainty <= _TOL:
        return _average_dogmatic(a, b, base_rate)
    kappa = a.uncertainty + b.uncertainty - a.uncertainty * b.uncertainty
    return Opinion(
        belief=(a.belief * b.uncertainty + b.belief * a.uncertainty) / kappa,
        disbelief=(a.disbelief * b.uncertainty + b.disbelief * a.uncertainty) / kappa,
        uncertainty=(a.uncertainty * b.uncertainty) / kappa,
        base_rate=base_rate,
    )


def averaging_fuse(a: Opinion, b: Opinion) -> Opinion:
    """Averaging (epistemic) belief fusion of two possibly-**dependent** opinions (Jøsang).

    Averages the opinions weighted by their relative certainty, *without* accumulating certainty:
    it is **idempotent** (fusing an opinion with itself returns it) and never yields more
    certainty than its most-certain input. This is what makes it safe under **correlated LLM
    error** (§13) — the reason it is :data:`DEFAULT_FUSION`. Formula (non-dogmatic case)::

        κ = uA + uB
        belief = (bA·uB + bB·uA) / κ ;  uncertainty = (2·uA·uB) / κ

    (It differs from cumulative only in κ and the uncertainty numerator — the difference between
    *averaging* and *accumulating* evidence.) Both dogmatic ⇒ the equal-weight belief average,
    the same documented limit as cumulative.
    """
    base_rate = _require_shared_base_rate(a, b)
    if a.uncertainty <= _TOL and b.uncertainty <= _TOL:
        return _average_dogmatic(a, b, base_rate)
    kappa = a.uncertainty + b.uncertainty
    return Opinion(
        belief=(a.belief * b.uncertainty + b.belief * a.uncertainty) / kappa,
        disbelief=(a.disbelief * b.uncertainty + b.disbelief * a.uncertainty) / kappa,
        uncertainty=(2.0 * a.uncertainty * b.uncertainty) / kappa,
        base_rate=base_rate,
    )


def _average_dogmatic(a: Opinion, b: Opinion, base_rate: float) -> Opinion:
    """The both-dogmatic limit shared by both fusions: equal-weight average of the beliefs.

    Two zero-uncertainty opinions carry no uncertainty mass to weight the average by, so the
    principled limit of the general weighted forms (with equal relative weights) is the plain
    average. Kept in one place so the two operators cannot diverge on this edge case.
    """
    return Opinion(
        belief=(a.belief + b.belief) / 2.0,
        disbelief=(a.disbelief + b.disbelief) / 2.0,
        uncertainty=0.0,
        base_rate=base_rate,
    )


@dataclass(frozen=True)
class Fusion:
    """A fusion operator as a value (mirroring ``GradualSemantics`` in ``core/qbaf.py``).

    Carries the operator's name (for ids/logging) and its binary ``fuse_pair`` function, so
    :func:`fuse` is written **once, generic over the operator**, and the default is swapped at
    the seam — not branched on. The two instances are :data:`CUMULATIVE` and :data:`AVERAGING`.
    """

    name: str
    fuse_pair: Callable[[Opinion, Opinion], Opinion]


CUMULATIVE = Fusion(name="cumulative", fuse_pair=cumulative_fuse)
AVERAGING = Fusion(name="averaging", fuse_pair=averaging_fuse)

# Decided eyes-open with the fixture (see module docstring + test_subjective_logic.py): averaging
# is idempotent under correlated evidence, so it cannot manufacture certainty from correlated LLM
# judgments (the §13 risk). Cumulative is retained at the seam for genuinely independent judges.
DEFAULT_FUSION: Fusion = AVERAGING


def fuse(opinions: Sequence[Opinion], *, fusion: Fusion = DEFAULT_FUSION) -> Opinion:
    """Fuse a sequence of opinions into one under ``fusion`` (step 4).

    Folds the binary operator left-to-right over the multi-sample (and multi-source) judgments
    of one edge. Both shipped operators are commutative, so the fold order does not affect the
    result (up to floating-point). A single opinion fuses to itself; an empty sequence raises —
    there is nothing to score, a caller bug rather than a silent vacuous default.
    """
    if not opinions:
        raise ValueError("fuse requires at least one opinion")
    result = opinions[0]
    for op in opinions[1:]:
        result = fusion.fuse_pair(result, op)
    return result
