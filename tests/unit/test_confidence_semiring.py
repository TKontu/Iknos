"""G3.5 — the Layer B semiring decision fixture (architecture §12, review A6).

§12 makes the **Viterbi `max-·` vs Gödel `max-min`** choice an explicit *Phase-3-entry
decision, made with a fixture before any Layer B valuation engine exists*, because the
choice is epistemic (what does "confidence" mean across derivation depth?), not a tuning
detail. This module is that fixture. It:

1. **demonstrates the depth bias numerically** — a deep chain vs a shallow one from
   equal-quality evidence, and the weakest-link behaviour — so the decision is taken with
   eyes open;
2. **pins the semiring laws** the G3.6 least-fixpoint engine relies on for cycle
   convergence (idempotence of ``⊕``, absorption, identities, commutativity/associativity,
   ``[0, 1]`` closure);
3. **records the decision as an executable assertion** — Gödel is the default and is
   depth-neutral where Viterbi is not.

The recorded decision (see ``docs/gap_phase_3_reasoning_core.md``): **Gödel `max-min` is
the Layer B default.** Pure, DB-free — a small algebra over toy acyclic graphs, evaluated
by the throwaway topological helper below (the *production* valuation is the cyclic least
fixpoint of G3.6; this fixture only needs acyclic evaluation to expose the bias).
"""

import itertools
import math

from iknos.core.confidence import (
    DEFAULT_SEMIRING,
    GODEL,
    VITERBI,
    Confidence,
    Semiring,
)

# A toy rule for the fixture: ``head`` is derivable from ``body`` antecedents with the
# given ``DERIVED_FROM`` edge ``strength`` (§7.1 — edges carry a strength, not a boolean).
type _Rule = tuple[str, tuple[str, ...], Confidence]


def _evaluate_acyclic(
    semiring: Semiring,
    base: dict[str, Confidence],
    rules: tuple[_Rule, ...],
) -> dict[str, Confidence]:
    """Confidence of every node in an **acyclic** toy graph under ``semiring``.

    Throwaway test scaffolding — *not* the production engine (that is G3.6's cyclic least
    fixpoint). A node's confidence is ``⊕`` across its derivations of
    ``strength ⊗ (⊗ body antecedents)``, combined with its base-evidence confidence if it
    is a base fact. Evaluated by repeated relaxation until a pass changes nothing; on an
    acyclic graph that reaches the fixpoint, and it matches what G3.6 must compute here.
    """
    conf: dict[str, Confidence] = dict(base)
    changed = True
    while changed:
        changed = False
        for head, body, strength in rules:
            if any(a not in conf for a in body):
                continue  # an antecedent has no value yet; a later pass will pick it up
            contribution = semiring.times(strength, semiring.combine_body(conf[a] for a in body))
            updated = semiring.plus(conf.get(head, semiring.zero), contribution)
            if head not in conf or updated != conf[head]:
                conf[head] = updated
                changed = True
    return conf


# --- 1. the depth-bias demonstration (the reason the decision is forced) ---------------


def test_viterbi_is_depth_biased_godel_is_depth_neutral() -> None:
    """The headline §12 fixture: equal evidence quality, different derivation depth.

    A deep chain ``f0 → f1 → … → f5`` (five derivation steps) and a shallow ``g0 → g1``
    (one step), both grounded in *certain* base facts, every derivation step a
    0.9-confidence ``DERIVED_FROM`` edge (§12's "five 0.9-confidence steps"). Under Viterbi
    the deep conclusion is geometrically punished (``0.9**5``) for being *derived
    carefully*; under Gödel it is exactly as strong as its weakest link, equal to the
    shallow one. This divergence is the whole decision.
    """
    base = {"f0": 1.0, "g0": 1.0}
    deep = tuple((f"f{i + 1}", (f"f{i}",), 0.9) for i in range(5))
    shallow: tuple[_Rule, ...] = (("g1", ("g0",), 0.9),)
    rules = deep + shallow

    viterbi = _evaluate_acyclic(VITERBI, base, rules)
    godel = _evaluate_acyclic(GODEL, base, rules)

    # Viterbi: depth bias — the deep conclusion decays geometrically and is strictly
    # weaker than the shallow one despite identical evidence quality.
    assert math.isclose(viterbi["f5"], 0.9**5, abs_tol=1e-12)
    assert math.isclose(viterbi["f5"], 0.59049, abs_tol=1e-9)
    assert viterbi["f5"] < viterbi["g1"]
    assert viterbi["g1"] == 0.9

    # Gödel: depth-neutral — deep and shallow conclusions are equal, both = the weakest
    # (here only) link. Depth carries no penalty.
    assert godel["f5"] == 0.9
    assert godel["g1"] == 0.9
    assert godel["f5"] == godel["g1"]


def test_godel_is_exactly_the_weakest_link() -> None:
    """Gödel ``min`` along a body: a chain is as strong as its single weakest antecedent,
    and adding more strong links never lowers it further (no compounding)."""
    base = {"a": 0.95}
    rules: tuple[_Rule, ...] = (
        ("b", ("a",), 0.4),  # a deliberately weak DERIVED_FROM edge — the bottleneck
        ("c", ("b",), 0.99),
        ("d", ("c",), 0.99),
    )
    godel = _evaluate_acyclic(GODEL, base, rules)
    viterbi = _evaluate_acyclic(VITERBI, base, rules)

    # Gödel pins the whole chain at the 0.4 bottleneck regardless of the strong links after.
    assert godel["b"] == 0.4
    assert godel["d"] == 0.4
    # Viterbi keeps eroding past the bottleneck — the depth bias compounding the weak edge.
    assert viterbi["d"] < 0.4


# --- 2. ⊕ = max picks the best derivation (a multi-path graph) -------------------------


def test_alternatives_take_the_best_derivation_under_both_semirings() -> None:
    """A node reachable by a strong short path and a weak long path: ``⊕`` = max keeps the
    better derivation under both semirings (so foundedness, not strength, decides which
    paths exist — that is Layer A's job; Layer B only scores)."""
    base = {"s": 0.9}
    rules: tuple[_Rule, ...] = (
        ("mid", ("s",), 0.5),  # weak intermediate on the long path
        ("h", ("s",), 0.8),  # short strong path:   s --0.8--> h
        ("h", ("mid",), 0.95),  # long weaker path:    s -> mid --0.95--> h
    )
    for semiring in (VITERBI, GODEL):
        conf = _evaluate_acyclic(semiring, base, rules)
        short = semiring.times(0.8, 0.9)
        long_path = semiring.times(0.95, semiring.times(0.5, 0.9))
        assert conf["h"] == max(short, long_path)


# --- 3. the semiring laws G3.6's cyclic fixpoint relies on -----------------------------

_SAMPLES = (0.0, 0.1, 0.4, 0.5, 0.9, 1.0)


def test_identities() -> None:
    for sr in (VITERBI, GODEL):
        for a in _SAMPLES:
            assert sr.times(a, sr.one) == a  # one is ⊗ identity (empty body)
            assert sr.plus(a, sr.zero) == a  # zero is ⊕ identity (no derivation)
            assert sr.times(a, sr.zero) == sr.zero  # zero absorbs under ⊗
            assert sr.plus(a, sr.one) == sr.one  # one is the ⊕ top


def test_commutativity_and_associativity() -> None:
    for sr in (VITERBI, GODEL):
        for a, b, c in itertools.product(_SAMPLES, repeat=3):
            assert sr.times(a, b) == sr.times(b, a)
            assert sr.plus(a, b) == sr.plus(b, a)
            # ⊗ associativity holds up to float rounding (Viterbi's product is not bit-exact
            # associative); ⊕ (max) is exact, but assert both the same lenient way.
            assert math.isclose(
                sr.times(sr.times(a, b), c), sr.times(a, sr.times(b, c)), abs_tol=1e-12
            )
            assert sr.plus(sr.plus(a, b), c) == sr.plus(a, sr.plus(b, c))


def test_plus_is_idempotent_and_pair_is_absorptive() -> None:
    """The two laws that make the G3.6 confidence least fixpoint converge on cyclic
    ``DERIVED_FROM`` graphs without inflation: ``a ⊕ a = a`` and ``a ⊕ (a ⊗ b) = a``."""
    for sr in (VITERBI, GODEL):
        for a in _SAMPLES:
            assert sr.plus(a, a) == a  # idempotent ⊕
            for b in _SAMPLES:
                assert sr.plus(a, sr.times(a, b)) == a  # absorption (a⊗b ≤ a on [0,1])


def test_operations_are_closed_and_monotone_on_the_unit_interval() -> None:
    for sr in (VITERBI, GODEL):
        for a, b in itertools.product(_SAMPLES, repeat=2):
            assert 0.0 <= sr.times(a, b) <= 1.0
            assert 0.0 <= sr.plus(a, b) <= 1.0
            assert sr.times(a, b) <= a  # ⊗ never strengthens (depth/conjunction weakens)
            assert sr.plus(a, b) >= a  # ⊕ never weakens (more derivations only help)


def test_combine_helpers_fold_with_the_right_identities() -> None:
    for sr in (VITERBI, GODEL):
        assert sr.combine_body(()) == sr.one  # empty body = axiom strength
        assert sr.combine_alternatives(()) == sr.zero  # no derivation = unsupported strength
        assert sr.combine_body((0.5, 0.8, 1.0)) == sr.times(sr.times(0.5, 0.8), 1.0)
        assert sr.combine_alternatives((0.2, 0.9, 0.5)) == 0.9


# --- 4. the decision, as an executable record -----------------------------------------


def test_default_semiring_is_godel_and_is_depth_neutral() -> None:
    """The recorded G3.5 decision: Gödel is the Layer B default, chosen because it is
    depth-neutral where Viterbi is not (proven above)."""
    assert DEFAULT_SEMIRING is GODEL
    assert DEFAULT_SEMIRING.name == "godel"
    # Depth-neutrality of the default, stated directly: any number of strength-1 min-steps
    # from a 0.9 base stays 0.9.
    base = {"x0": 0.9}
    chain = tuple((f"x{i + 1}", (f"x{i}",), 1.0) for i in range(8))
    conf = _evaluate_acyclic(DEFAULT_SEMIRING, base, chain)
    assert conf["x8"] == 0.9
