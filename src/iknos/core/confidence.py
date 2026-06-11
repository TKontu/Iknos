"""Layer B — confidence valuation over a semiring (G3.5 algebra + G3.6 engine; §12, §7.1).

Layer B owns **strength**. On exactly the nodes Layer A (``core/truth_maintenance.py``)
certifies as *well-founded*-supported, it computes a ``[0, 1]`` confidence as a least
fixpoint over an **absorptive, ω-continuous semiring**. The two annotations are never
merged (§12): Layer A's integer support-count answers *which* / *by how many derivations*;
Layer B's confidence answers *how strongly*. Conflating them reintroduces the exact bug
the split exists to avoid — confidence that double-counts or fails to converge on cycles.

This module ships, in order: the **algebra** (:class:`Semiring` + the two candidates,
G3.5), and the **valuation engine** (:func:`valuate`, G3.6) — the cycle-convergent least
fixpoint over the chosen semiring, **gated on Layer A's certified set** so an unfounded
cycle never receives a confidence (§12: "foundedness gates confidence").

**The semiring is an explicit Phase-3-entry decision (G3.5), not a default.** §12 mandates
the **Viterbi `max-·` vs Gödel `max-min`** choice be made *with a fixture, before* the
valuation engine, because it is an **epistemic** choice, not a tuning detail:

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
zero, one)`` — so :func:`valuate` is written **once, generic over a** :class:`Semiring`,
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

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from iknos.core.truth_maintenance import Derivation, DerivationGraph, NodeId

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

#: The Layer B default the G3.5 fixture decided on. :func:`valuate` takes a
#: :class:`Semiring` argument defaulting to this, so the choice stays reversible at the seam.
DEFAULT_SEMIRING = GODEL


def valuate(
    graph: DerivationGraph,
    supported: frozenset[NodeId],
    *,
    base_confidence: Mapping[NodeId, Confidence] | None = None,
    strength: Mapping[Derivation, Confidence] | None = None,
    semiring: Semiring = DEFAULT_SEMIRING,
) -> dict[NodeId, Confidence]:
    """G3.6 — Layer B confidence valuation: the least fixpoint over ``semiring``, computed
    **only over the Layer-A-certified** ``supported`` set (§12).

    Returns ``{node: confidence}`` for **exactly** the supported nodes. ``supported`` must be
    Layer A's well-founded set for this same ``graph`` (obtained from a
    :class:`~iknos.core.truth_maintenance.SupportOracle`) — that is the two-layer seam: Layer
    A decides *membership*, Layer B scores it. Passing the certified set in (rather than
    recomputing it here) keeps the layers' annotations cleanly separate and the engine pure.

    **The valuation.** A node's confidence is the semiring sum (``⊕``, best derivation)
    over its grounds:

    * if it is a base fact, the evidence confidence ``base_confidence[node]`` (the
      ``EVIDENCED_BY`` strength; missing ⇒ ``semiring.one``, a certain leaf);
    * for each of its derivations, the ``DERIVED_FROM`` edge ``strength[d]`` (§7.1; missing
      ⇒ ``one``) combined (``⊗``) with the body product (``⊗`` of the antecedents'
      confidences).

    **Foundedness gates confidence (§12).** Only derivations whose head *and whole body* are
    in ``supported`` can contribute. An unfounded cycle is absent from ``supported``, so it is
    **never scored** — Layer A's membership decision, taken first, is what keeps the cycle out
    of Layer B. (Layer B *would* converge on it; convergence is not foundedness.)

    **Convergence.** Computed by Kleene ascent (Jacobi iteration) from ``zero``. ``⊕`` is
    idempotent and the semiring absorptive (``a ⊕ (a ⊗ b) = a``, since ``a ⊗ b ≤ a`` on
    ``[0, 1]``) and ω-continuous, so the iterates are monotone, bounded by ``one``, and reach
    the least fixpoint on **acyclic and cyclic** ``DERIVED_FROM`` graphs alike — a cyclic
    contribution can never exceed the node's direct grounding, so a grounded cycle saturates
    instead of inflating. The iteration is bounded; exceeding the bound (which an absorptive,
    ω-continuous semiring cannot do) raises rather than hangs — the inner-layer analogue of
    §12's composed-loop iteration bound. *(The genuinely separate composed-loop oscillation
    detection — REFUTES→retract→A→B→QBAF — is G3.9.)*
    """
    base_conf: Mapping[NodeId, Confidence] = base_confidence or {}
    edge_strength: Mapping[Derivation, Confidence] = strength or {}

    # Foundedness gate: index only the derivations both of whose endpoints Layer A certifies.
    grounds: dict[NodeId, list[Derivation]] = defaultdict(list)
    for d in graph.derivations:
        if d.conclusion in supported and all(a in supported for a in d.body):
            grounds[d.conclusion].append(d)

    nodes = sorted(supported)  # deterministic iteration ⇒ replay-stable trace (§10)
    conf: dict[NodeId, Confidence] = dict.fromkeys(nodes, semiring.zero)

    def contributions(node: NodeId) -> Iterable[Confidence]:
        if node in graph.base_facts:
            yield base_conf.get(node, semiring.one)
        for d in grounds[node]:
            body = semiring.combine_body(conf[a] for a in d.body)
            yield semiring.times(edge_strength.get(d, semiring.one), body)

    # The longest grounding chain through `supported` is < len(nodes); Jacobi propagates one
    # edge per round, plus one round to confirm the fixpoint. Cycles add no rounds (absorption
    # caps them at their direct grounding). +2 is therefore a safe ceiling, not a tuning knob.
    for _ in range(len(nodes) + 2):
        nxt = {n: semiring.combine_alternatives(contributions(n)) for n in nodes}
        if nxt == conf:
            return conf
        conf = nxt
    raise RuntimeError(  # pragma: no cover — unreachable for an absorptive, ω-continuous ⊕
        "Layer B confidence valuation did not converge; the semiring is not "
        "absorptive/ω-continuous as §12 requires."
    )
