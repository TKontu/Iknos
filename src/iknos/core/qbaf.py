"""Adjudication — QBAF gradual semantics + hypothesis state (G4.1; §7.2, §8, §11.2, §13).

Phase 4 turns graded ``SUPPORTS``/``REFUTES`` evidence into a **hypothesis verdict**. The
architecture (§8) fixes the model: a **Quantitative Bipolar Argumentation Framework (QBAF)**
with a *gradual* semantics, which yields a real-valued **acceptability** in ``[0, 1]`` from
each argument's **intrinsic base score** plus its incoming attack/support — *not* a boolean
Dung extension. This is the seam after propagation: Layer A (``truth_maintenance``) decides
well-founded *membership*, Layer B (``confidence.valuate``) scores it, and **this module
adjudicates** supports/refutes over those scores (§12: "Layer B's ``[0,1]`` confidence is the
clean strength consumed as a node's intrinsic/base score by the QBAF gradual semantics").

This increment is the **pure adjudication core**, mirroring how Layer B was built — the
**semantics decision first** (a fixture, before the engine, because the choice is epistemic),
then the engine generic over it, then the verdict/state read-off. No DB, no AGE, no LLM, no
migration: a small dynamical system over ``[0, 1]`` floats, unit-testable in isolation exactly
like ``core/confidence.py``. Candidate generation (§5.1), the LLM edge-judgment pipeline (§8),
and persistence to AGE are the later Phase-4 increments that *feed* this engine; the ensemble
gate (§7.2) that authorises a persisted ``refuted`` flip is a documented seam below.

**The semantics is an explicit decision (G4.1), not a default.** §8 names two gradual
semantics — **DF-QuAD** and **Quadratic Energy** — and they differ in how multiple pieces of
evidence on one hypothesis combine:

* **DF-QuAD** (Rago–Toni et al.) aggregates supporters (and, separately, attackers) by the
  **probabilistic sum** ``a ⊕ b = a + b − a·b`` — an "at least one independent reason holds"
  combinator that **saturates**: once aggregate support is near ``1``, further equally-strong
  supporters add almost nothing. The combination with the base score is **discontinuity-free**
  and stays in ``[0, 1]`` by construction.
* **Quadratic Energy** (Potyka) aggregates by the **plain sum** of contributions into an
  *energy* ``E = Σ support − Σ attack``, then squashes with ``φ(x) = x²/(1+x²)``. Support
  **accrues**: many independent weak reasons keep raising acceptability (with diminishing but
  non-zero marginal effect), and it has a well-studied convergence theory.

**Decision, recorded eyes-open: DF-QuAD is the Phase-4 default.** Saturation is the
*conservative* choice under the project's standing §13 risk that **correlated LLM error is not
removed by the edge-judgment disciplines** — DF-QuAD will not let many correlated / duplicated
weak "supports" manufacture a high acceptability, whereas Quadratic Energy's linear accrual
would amplify exactly that failure mode. It is also bounded and discontinuity-free *by
construction* (acyclic frameworks need no tolerance at all), and ordering-preserving, matching
§8's "the gradual semantics depends mostly on ordering" and the ordinal Gödel Layer B that
feeds it. This parallels the Layer B choice (Gödel over Viterbi): default to the algebra that
**cannot inflate**, retain the other at the seam. **Quadratic Energy is retained** as a
selectable value for a sub-domain where genuine independent corroboration *should* accrue (and
upstream subjective-logic fusion has already decorrelated the evidence), so the choice stays
reversible — exactly the G3.5 discipline. The fixture in
``tests/unit/test_qbaf_semantics.py`` demonstrates the saturation-vs-accrual divergence
numerically, so this is a decision and not an accident.

Both share one shape — ``aggregate`` a bag of incoming contributions, then ``combine`` the
base score with aggregate support and aggregate attack — so :func:`solve` is written **once,
generic over a** :class:`GradualSemantics`, with the default swapped at this seam.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

# An argument id. The pure core is id-blind exactly like ``truth_maintenance.NodeId``: the
# Phase-4 adapter (a later increment) stringifies AGE/UUID ids at the boundary.
type ArgId = str

# A strength / acceptability is an ordinal degree in the closed unit interval — the same
# ``[0, 1]`` currency Layer B produces (§12), *not* a calibrated probability.
type Strength = float


# --------------------------------------------------------------------------------------------
# The bipolar argumentation framework (the pure, in-memory structure — analogue of
# ``truth_maintenance.DerivationGraph``).
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    """A weighted bipolar edge: ``src`` supports/attacks ``dst`` with ``strength`` (§7.1).

    ``strength`` is the **calibrated, adjudicated** edge weight (the fused/recalibrated value
    of §8/§10 — never a raw LLM number), in ``[0, 1]``. The *sign* (support vs attack) is
    carried by which collection the edge lives in (:attr:`BAF.supports` / :attr:`BAF.attacks`),
    honouring §8's "sign before magnitude": direction is a separate, categorical fact from
    magnitude, so it is modelled structurally rather than as a signed number.
    """

    src: ArgId
    dst: ArgId
    strength: Strength


@dataclass(frozen=True)
class BAF:
    """A Quantitative Bipolar Argumentation Framework: arguments + weighted support/attack.

    Pure and id-blind. An argument's **base score** (its intrinsic weight = the Layer B
    confidence of the underlying claim, §12) is supplied to :func:`solve` as a side map, never
    stored here — mirroring how ``confidence.valuate`` takes ``base_confidence`` as an argument
    so the structure stays independent of the scoring. An argument absent from the base map
    defaults to ``0.0`` (no intrinsic support until evidenced).

    ``supports`` and ``attacks`` are tuples (deterministic order) of :class:`Edge`. An edge
    whose ``src`` or ``dst`` is not in :attr:`arguments` is **ignored** by :func:`solve` (a
    dangling edge cannot lend or remove strength) — partial-tolerant by the same discipline as
    the derivation adapter.
    """

    arguments: frozenset[ArgId]
    supports: tuple[Edge, ...] = ()
    attacks: tuple[Edge, ...] = ()


# --------------------------------------------------------------------------------------------
# The gradual semantics (the algebra — analogue of ``confidence.Semiring``; the values are
# DF_QUAD / QUADRATIC_ENERGY, selected at a seam, not branched on).
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GradualSemantics:
    """A gradual semantics ``(aggregate, combine)`` over ``[0, 1]`` acceptabilities.

    Two hooks, paired:

    * ``aggregate`` folds a bag of **per-edge contributions** (each ``edge.strength · σ(src)``
      — the edge weight modulating the supporter's/attacker's own current strength) into a
      single quantity. DF-QuAD uses the probabilistic sum (bounded in ``[0, 1]``); Quadratic
      Energy uses the plain sum (an unbounded energy ``≥ 0``). Order-independent (commutative),
      so :func:`solve` may feed contributions in any deterministic order.
    * ``combine`` produces an argument's new strength from its base score and the two
      aggregates ``(base, aggregate_support, aggregate_attack)``. Both candidates return a
      value in ``[0, 1]`` and satisfy **balance** (equal aggregate support and attack ⇒ the
      base score is returned unchanged) and **stability** (no support and no attack ⇒ the base
      score).

    The two are a matched pair (DF-QuAD's ``combine`` expects ``[0, 1]`` aggregates from its
    probabilistic-sum ``aggregate``; Quadratic Energy's expects energy sums), stored as plain
    functions so the algebra is a **value** and the engine selects one at a seam.
    """

    name: str
    aggregate: Callable[[Iterable[Strength]], Strength]
    combine: Callable[[Strength, Strength, Strength], Strength]


def _prob_sum(contributions: Iterable[Strength]) -> Strength:
    """Probabilistic sum ``1 − Π(1 − c)`` — "at least one independent reason holds". Saturates
    toward ``1`` and stays in ``[0, 1]``; identity ``0`` for no contributions."""
    acc = 0.0
    for c in contributions:
        acc = acc + c - acc * c
    return acc


def _df_quad_combine(base: Strength, support: Strength, attack: Strength) -> Strength:
    """DF-QuAD combination of a base score with aggregate support/attack (both already in
    ``[0, 1]`` from :func:`_prob_sum`). Net support raises the base toward ``1``; net attack
    lowers it toward ``0``; a tie leaves it unchanged (balance). Discontinuity-free, ``[0, 1]``
    closed."""
    if support > attack:
        return base + (1.0 - base) * (support - attack)
    if attack > support:
        return base - base * (attack - support)
    return base


def _plain_sum(contributions: Iterable[Strength]) -> Strength:
    """Plain sum of contributions — the Quadratic-Energy *energy* term (unbounded ``≥ 0``)."""
    return sum(contributions)


def _phi(x: Strength) -> Strength:
    """Quadratic squashing ``x²/(1+x²)`` on ``x ≥ 0`` — maps an energy magnitude into
    ``[0, 1)`` with diminishing marginal effect."""
    sq = x * x
    return sq / (1.0 + sq)


def _quadratic_energy_combine(base: Strength, support: Strength, attack: Strength) -> Strength:
    """Quadratic-Energy combination (Potyka): energy ``E = support − attack`` squashed by
    :func:`_phi`; net positive energy raises the base toward ``1``, net negative lowers it
    toward ``0``. Balance and ``[0, 1]`` closure hold; support **accrues** rather than
    saturating."""
    energy = support - attack
    if energy >= 0.0:
        return base + (1.0 - base) * _phi(energy)
    return base - base * _phi(-energy)


#: DF-QuAD — probabilistic-sum aggregation, discontinuity-free combination. **Saturating**
#: (conservative under correlated evidence) and bounded by construction. The recorded Phase-4
#: **default** (see module docstring).
DF_QUAD = GradualSemantics(name="df-quad", aggregate=_prob_sum, combine=_df_quad_combine)

#: Quadratic Energy (Potyka) — plain-sum energy, quadratic squashing. **Accruing** (independent
#: corroboration keeps raising acceptability). Retained as a ready instance for a decorrelated
#: sub-domain; **not** the default — the choice stays reversible at the seam.
QUADRATIC_ENERGY = GradualSemantics(
    name="quadratic-energy", aggregate=_plain_sum, combine=_quadratic_energy_combine
)

#: The Phase-4 default the G4.1 fixture decided on. :func:`solve` takes a
#: :class:`GradualSemantics` argument defaulting to this, so the choice stays reversible.
DEFAULT_SEMANTICS = DF_QUAD


# --------------------------------------------------------------------------------------------
# The engine — the gradual-semantics fixpoint, bounded with non-convergence surfaced (§13).
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QbafResult:
    """The outcome of :func:`solve`.

    ``acceptability`` is ``{argument: strength}`` for **every** argument in the framework (the
    real-valued QBAF strength §11.2 bands into a verdict). ``converged`` is whether the
    iteration reached the fixpoint within ``tolerance`` before the bound — ``True`` always for
    an **acyclic** framework, possibly ``False`` for a cyclic one (§13: cyclic argument graphs
    have no general convergence guarantee). ``iterations`` counts the update sweeps performed.
    ``unstable`` is the set of arguments still moving by more than ``tolerance`` when the bound
    was hit — empty when converged; **non-empty it is the unresolved region to surface as a
    finding** (§7.2, §13: "present the region as unresolved with its subgraph — not guarantee a
    fixed point", never smoothed into a false verdict).
    """

    acceptability: Mapping[ArgId, Strength]
    converged: bool
    iterations: int
    unstable: frozenset[ArgId] = field(default_factory=frozenset)

    @property
    def is_finding(self) -> bool:
        """Whether this is an **unresolved region to surface** (did not converge) rather than a
        settled adjudication (§13 — surface, don't smooth into a false verdict)."""
        return not self.converged


def _check_unit_interval(value: Strength, what: str) -> None:
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"{what} must be in [0, 1], got {value!r}")


def solve(
    baf: BAF,
    *,
    base: Mapping[ArgId, Strength] | None = None,
    semantics: GradualSemantics = DEFAULT_SEMANTICS,
    max_iterations: int = 100,
    tolerance: Strength = 1e-9,
) -> QbafResult:
    """Compute QBAF acceptability by iterating ``semantics`` to a tolerance-bounded fixpoint.

    Each sweep recomputes, for every argument ``a``, its new strength from its **base score**
    (``base[a]``, the Layer B confidence; missing ⇒ ``0.0``) and the ``aggregate`` of its
    incoming **support** and **attack** contributions, where one edge contributes
    ``edge.strength · σ(src)`` — the §7.1 edge weight modulating the source's *current*
    strength (so a weak edge, or a weak source, lends little). Updates are **synchronous**
    (Jacobi): a sweep reads the previous sweep's strengths, so the result is independent of
    argument order (anonymity).

    Convergence is by max-change ``< tolerance``. An **acyclic** framework reaches the exact
    fixpoint in at most depth-many sweeps and always converges; a **cyclic** one (mutual
    ``SUPPORTS``/``REFUTES``, §13) may converge to a saturated fixpoint or, lacking a general
    guarantee, hit ``max_iterations`` — in which case the still-moving arguments are returned
    in :attr:`QbafResult.unstable` for the caller to surface as an unresolved finding (§7.2),
    **never** silently re-iterated past the bound or reported as a settled verdict. This is the
    inner-numeric analogue of the outer composed-loop driver
    (``core/composed_loop.py::stabilize``, G3.9): there discrete states and exact recurrence;
    here continuous strengths and a tolerance.

    Base scores and edge strengths must be in ``[0, 1]`` (raises :class:`ValueError`
    otherwise) — a cheap boundary check, since the combination math assumes the unit interval.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    base_score: Mapping[ArgId, Strength] = base or {}
    for arg, score in base_score.items():
        _check_unit_interval(score, f"base score for {arg!r}")

    args = sorted(baf.arguments)  # deterministic iteration ⇒ replay-stable trace (§10)

    # Index incoming edges by destination, dropping dangling endpoints (partial-tolerant).
    def incoming(edges: tuple[Edge, ...]) -> dict[ArgId, list[Edge]]:
        by_dst: dict[ArgId, list[Edge]] = {a: [] for a in args}
        for e in edges:
            _check_unit_interval(e.strength, f"edge strength {e.src!r}->{e.dst!r}")
            if e.src in baf.arguments and e.dst in baf.arguments:
                by_dst[e.dst].append(e)
        return by_dst

    in_support = incoming(baf.supports)
    in_attack = incoming(baf.attacks)

    # Seed at the base scores (the §8 intrinsic weight) — the natural starting point and the
    # exact fixpoint for an argument with no incoming edges (stability).
    sigma: dict[ArgId, Strength] = {a: base_score.get(a, 0.0) for a in args}

    def sweep(prev: dict[ArgId, Strength]) -> dict[ArgId, Strength]:
        nxt: dict[ArgId, Strength] = {}
        for a in args:
            supp = semantics.aggregate(e.strength * prev[e.src] for e in in_support[a])
            att = semantics.aggregate(e.strength * prev[e.src] for e in in_attack[a])
            nxt[a] = semantics.combine(base_score.get(a, 0.0), supp, att)
        return nxt

    for i in range(1, max_iterations + 1):
        nxt = sweep(sigma)
        delta = max((abs(nxt[a] - sigma[a]) for a in args), default=0.0)
        sigma = nxt
        if delta <= tolerance:
            return QbafResult(acceptability=sigma, converged=True, iterations=i)

    # Bound reached without convergence — surface the still-moving region (§13), don't re-loop.
    last = sweep(sigma)
    unstable = frozenset(a for a in args if abs(last[a] - sigma[a]) > tolerance)
    return QbafResult(
        acceptability=sigma,
        converged=False,
        iterations=max_iterations,
        unstable=unstable,
    )


# --------------------------------------------------------------------------------------------
# Read-off — acceptability → verdict band (§11.2) and computed hypothesis state (§7.2, §10).
# --------------------------------------------------------------------------------------------


class Verdict(StrEnum):
    """The graded verdict a hypothesis's acceptability **bands** into for presentation (§11.2).
    Ordinal, not boolean — the whole point of gradual adjudication."""

    TRUE = "true"
    PLAUSIBLE = "plausible"
    IMPLAUSIBLE = "implausible"
    FALSE = "false"


class HypothesisState(StrEnum):
    """A hypothesis's computed state (§7.2, §10): **never hand-set** — derived from the QBAF.

    ``supported`` clears the acceptability bar; below it, ``refuted`` means *actively
    out-attacked* (net attack dominates) while ``unsupported`` means merely *lacking* support.
    The distinction matters: a refuted hypothesis has evidence against it; an unsupported one
    just has too little for it.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    REFUTED = "refuted"


@dataclass(frozen=True)
class VerdictBands:
    """Acceptability cut-points for the §11.2 verdict bands (and the supported/refuted bar).

    Defaults are **placeholders to be calibrated at the validation gate** (§8 experiment),
    not tuned constants — held as data (swappable at the same seam as the semantics) precisely
    so calibration re-points them without touching the engine. ``support_min`` is the bar a
    hypothesis must clear to count as :attr:`HypothesisState.SUPPORTED`; it defaults to
    ``plausible_min`` (a verdict at or above "plausible" is a supported hypothesis).
    """

    true_min: Strength = 0.75
    plausible_min: Strength = 0.5
    implausible_min: Strength = 0.25
    support_min: Strength | None = None  # None ⇒ use plausible_min

    def band(self, acceptability: Strength) -> Verdict:
        """Band an acceptability into a §11.2 verdict."""
        if acceptability >= self.true_min:
            return Verdict.TRUE
        if acceptability >= self.plausible_min:
            return Verdict.PLAUSIBLE
        if acceptability >= self.implausible_min:
            return Verdict.IMPLAUSIBLE
        return Verdict.FALSE


#: The default verdict bands (calibration targets, §8 experiment) — swappable like the
#: semantics seam.
DEFAULT_BANDS = VerdictBands()


def classify_state(
    *,
    acceptability: Strength,
    aggregate_support: Strength,
    aggregate_attack: Strength,
    bands: VerdictBands = DEFAULT_BANDS,
) -> HypothesisState:
    """Compute a hypothesis's state from its QBAF outputs (§7.2, §10) — never hand-set.

    Clears the support bar (``bands.support_min``, default ``plausible_min``) ⇒
    :attr:`HypothesisState.SUPPORTED`. Otherwise, net attack dominating support ⇒
    :attr:`HypothesisState.REFUTED` (actively out-argued); else
    :attr:`HypothesisState.UNSUPPORTED` (merely insufficient support).

    **Ensemble gate (§7.2) is the deferred seam.** §7.2 mandates that a flip *to* ``refuted``
    be authorised by the **ensemble gate** (multi-sample LLM + symbolic + temporal agreement),
    never a single judgment. This function computes the *structural* state the QBAF implies;
    persisting a ``refuted`` flip is gated on that ensemble agreement, which is LLM-/data-bound
    and lands with the §8 edge-judgment pipeline (a later Phase-4 increment). So the returned
    ``REFUTED`` is the engine's finding, the gate is the authorisation to act on it.
    """
    support_min = bands.support_min if bands.support_min is not None else bands.plausible_min
    if acceptability >= support_min:
        return HypothesisState.SUPPORTED
    if aggregate_attack > aggregate_support:
        return HypothesisState.REFUTED
    return HypothesisState.UNSUPPORTED
