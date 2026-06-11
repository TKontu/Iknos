"""Layer B — confidence valuation: the semiring algebra (G3.5, architecture §12, §7.1).

Layer B owns **strength**. On exactly the nodes Layer A (``core/truth_maintenance.py``)
certifies as *well-founded*-supported, it computes a ``[0, 1]`` confidence as a least
fixpoint over an **absorptive, ω-continuous semiring**. The two annotations are never
merged (§12): Layer A's integer support-count answers *which* / *by how many derivations*;
Layer B's confidence answers *how strongly*. Conflating them reintroduces the exact bug
the split exists to avoid — confidence that double-counts or fails to converge on cycles.

**This module is the algebra, not the engine.** §12 mandates that the
**Viterbi `max-·` vs Gödel `max-min`** choice be made *with a fixture, before* the Layer B
valuation engine (G3.6) is written, because it is an **epistemic** choice, not a tuning
detail:

* **Viterbi** ``([0,1], max, ·, 0, 1)`` multiplies confidences along a rule body. It
  carries a structural **depth bias** — confidence decays geometrically with derivation
  depth (five 0.9-confidence steps → ``0.9**5 ≈ 0.59`` *regardless of evidence quality*),
  so a deep, careful derivation is punished relative to a shallow one and the meaning of an
  acceptability band (§11.2) drifts with chain length. Keeping Viterbi would force the
  banding to be made depth-aware — strictly more machinery.
* **Gödel** ``([0,1], max, min, 0, 1)`` takes the weakest link along a body. It is
  **depth-neutral** — a chain is exactly as strong as its weakest antecedent — which
  matches the *ordinal*, ordering-driven use the QBAF gradual semantics (§8) makes of these
  scores downstream.

Both share one shape — ``(carrier, ⊕ across alternative derivations, ⊗ along a rule body,
zero, one)`` — so the G3.6 engine is written **once, generic over a** :class:`Semiring`,
and the chosen default is swapped at this seam rather than rewritten. Both ``⊕`` are
``max`` (idempotent), and both semirings are **absorptive** (``a ⊕ (a ⊗ b) = a`` since
``a ⊗ b ≤ a`` on ``[0, 1]``) and ω-continuous, so the confidence least fixpoint is
well-defined and **convergent even on cyclic derivation graphs**, double-counting-free
across alternative derivations by construction. The probabilistic **sum-product** semiring
is deliberately *not* offered here: it double-counts and can diverge on cycles unless
derivations are provably independent (§12).

The decision recorded by the fixture (``tests/unit/test_confidence_semiring.py``) and the
gap doc is **Gödel `max-min` as the Layer B default**; Viterbi is retained as a ready
instance for any future box whose degrees are genuinely probability-like rather than
ordinal (§12's parenthetical), so the choice stays reversible at the seam.

Deliberately **pure**: no DB, no AGE, no LLM — a small algebra over ``[0, 1]`` floats,
unit-testable in isolation exactly like ``core/truth_maintenance.py``.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

# A confidence is an ordinal degree in the closed unit interval. It is *not* a calibrated
# probability (§12: calibrated probabilities under correlated derivations would need
# probabilistic-database lineage, out of scope) — it is the strength the QBAF consumes.
type Confidence = float


@dataclass(frozen=True)
class Semiring:
    """A confidence semiring ``(carrier=[0,1], ⊕=plus, ⊗=times, zero, one)``.

    The two operations carry the two ways confidence composes in a derivation graph:

    * ``times`` (``⊗``) combines the confidences **along one rule body** — a conjunction;
      its identity ``one`` is the value of an empty body (an axiom is as strong as it gets).
    * ``plus`` (``⊕``) combines the confidences **across the alternative derivations** of a
      single node — a disjunction (best-derivation strength); its identity ``zero`` is the
      confidence of a node with no satisfied derivation.

    Required laws (both candidates satisfy them; checked in the fixture): both operations
    associative and commutative with the stated identities; ``⊕`` **idempotent**
    (``a ⊕ a = a``) and the pair **absorptive** (``a ⊕ (a ⊗ b) = a``); both **monotone**
    and **closed on ``[0, 1]``**. Idempotence + absorption are what make the G3.6 least
    fixpoint converge on cyclic ``DERIVED_FROM`` graphs without inflating confidence.

    The operations are stored as plain binary functions (not methods) so distinct algebras
    are values — :data:`VITERBI` and :data:`GODEL` differ only in their ``times`` — and the
    engine selects one at a seam instead of branching on a kind.
    """

    name: str
    times: Callable[[Confidence, Confidence], Confidence]  # ⊗ along a rule body
    plus: Callable[[Confidence, Confidence], Confidence]  # ⊕ across derivations
    one: Confidence  # ⊗ identity (empty body) and ⊕ absorbing top
    zero: Confidence  # ⊕ identity (no derivation) and ⊗ absorbing bottom

    def combine_body(self, values: Iterable[Confidence]) -> Confidence:
        """Fold ``⊗`` over a rule body's antecedent confidences (``one`` for an empty body).

        Order-independent (``⊗`` is associative and commutative), so the engine may feed
        antecedents in any deterministic order without changing the result.
        """
        acc = self.one
        for value in values:
            acc = self.times(acc, value)
        return acc

    def combine_alternatives(self, values: Iterable[Confidence]) -> Confidence:
        """Fold ``⊕`` over a node's alternative-derivation confidences (``zero`` for none).

        Idempotent and absorptive, so re-presenting the same derivation — as a cyclic
        fixpoint iteration does — cannot inflate the result past its supremum.
        """
        acc = self.zero
        for value in values:
            acc = self.plus(acc, value)
        return acc


def _viterbi_times(a: Confidence, b: Confidence) -> Confidence:
    return a * b


def _godel_times(a: Confidence, b: Confidence) -> Confidence:
    return min(a, b)


#: Viterbi ``([0,1], max, ·, 0, 1)`` — best-derivation strength, multiplying along a body.
#: Probability-like but **depth-biased** (geometric decay with derivation depth). Retained
#: for future probability-like boxes; **not** the Layer B default (see module docstring).
VITERBI = Semiring(name="viterbi", times=_viterbi_times, plus=max, one=1.0, zero=0.0)

#: Gödel / fuzzy ``([0,1], max, min, 0, 1)`` — best-derivation strength, weakest link along
#: a body. **Depth-neutral**; the recorded **Layer B default** (§12), matching the ordinal
#: use the QBAF makes of these scores downstream.
GODEL = Semiring(name="godel", times=_godel_times, plus=max, one=1.0, zero=0.0)

#: The Layer B default the G3.5 fixture decided on. The G3.6 valuation engine takes a
#: :class:`Semiring` argument defaulting to this, so the choice stays reversible at the seam.
DEFAULT_SEMIRING = GODEL
