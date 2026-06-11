"""G4.1 — the gradual-semantics decision fixture + the properties the engine relies on.

Mirrors ``test_confidence_semiring.py`` (the G3.5 semiring fixture): the **decision is made
numerically, before trusting the engine**. The headline shows DF-QuAD and Quadratic Energy
**rank the same two hypotheses oppositely** — that divergence *is* the epistemic choice (§8) —
and the rest pin the gradual-argumentation properties (stability, balance, neutrality,
monotonicity, boundedness, anonymity) both semantics must satisfy.
"""

import pytest

from iknos.core.qbaf import (
    BAF,
    DF_QUAD,
    QUADRATIC_ENERGY,
    Edge,
    GradualSemantics,
    solve,
)

BOTH: tuple[GradualSemantics, ...] = (DF_QUAD, QUADRATIC_ENERGY)


# --------------------------------------------------------------------------------------------
# The decision: one strong reason vs several weak ones — ranked OPPOSITELY by the two semantics
# --------------------------------------------------------------------------------------------


def test_decision_fixture_semantics_rank_one_strong_vs_many_weak_oppositely() -> None:
    """The headline (the §8 epistemic choice, demonstrated numerically).

    ``hA`` has **one strong** supporter (contribution 0.9); ``hB`` has **three weak** ones
    (contribution 0.4 each). DF-QuAD's probabilistic-sum aggregation **saturates**, so the
    single strong reason wins: ``hA > hB``. Quadratic Energy's plain-sum **accrues total
    mass**, so the three weak reasons win: ``hB > hA``. The ranking *flips* between the two —
    which is exactly why §8 forces the choice to be made with a fixture, and why DF-QuAD is the
    conservative default: three weak (possibly correlated) "supports" should not out-rank one
    genuinely strong piece of evidence (the standing §13 correlated-error risk).
    """
    # Leaf supporters (base 1.0, no incoming edges) sit at strength 1.0 by stability, so each
    # edge contributes exactly its strength.
    baf = BAF(
        arguments=frozenset({"hA", "hB", "pA", "pB1", "pB2", "pB3"}),
        supports=(
            Edge("pA", "hA", 0.9),
            Edge("pB1", "hB", 0.4),
            Edge("pB2", "hB", 0.4),
            Edge("pB3", "hB", 0.4),
        ),
    )
    base = {"pA": 1.0, "pB1": 1.0, "pB2": 1.0, "pB3": 1.0}  # hypotheses default to 0.0

    df = solve(baf, base=base, semantics=DF_QUAD).acceptability
    qe = solve(baf, base=base, semantics=QUADRATIC_ENERGY).acceptability

    # DF-QuAD: one strong reason beats three weak ones.
    assert df["hA"] == pytest.approx(0.9)
    assert df["hB"] == pytest.approx(1 - 0.6**3)  # prob-sum of three 0.4s = 0.784
    assert df["hA"] > df["hB"]

    # Quadratic Energy: accrued weak mass beats the single strong reason — ranking flips.
    assert qe["hA"] == pytest.approx(0.9**2 / (1 + 0.9**2))  # φ(0.9) ≈ 0.4475
    assert qe["hB"] == pytest.approx(1.2**2 / (1 + 1.2**2))  # φ(1.2) ≈ 0.5902
    assert qe["hB"] > qe["hA"]


def test_df_quad_saturates_a_second_certain_supporter_adds_nothing() -> None:
    """DF-QuAD's probabilistic sum pins aggregate support at 1.0 once one contribution is
    certain — a second certain, independent supporter cannot raise it further (the saturation
    that makes it conservative). Quadratic Energy, by contrast, keeps accruing."""
    one = BAF(arguments=frozenset({"h", "p1"}), supports=(Edge("p1", "h", 1.0),))
    two = BAF(
        arguments=frozenset({"h", "p1", "p2"}),
        supports=(Edge("p1", "h", 1.0), Edge("p2", "h", 1.0)),
    )
    base = {"p1": 1.0, "p2": 1.0, "h": 0.3}

    df1 = solve(one, base=base).acceptability["h"]
    df2 = solve(two, base=base).acceptability["h"]
    assert df1 == pytest.approx(df2)  # saturated: the second certain supporter adds nothing

    qe1 = solve(one, base=base, semantics=QUADRATIC_ENERGY).acceptability["h"]
    qe2 = solve(two, base=base, semantics=QUADRATIC_ENERGY).acceptability["h"]
    assert qe2 > qe1  # accrual: the second supporter still raises it


# --------------------------------------------------------------------------------------------
# Properties both semantics must satisfy (the "laws" analogue of the semiring fixture)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sem", BOTH, ids=lambda s: s.name)
def test_stability_no_edges_returns_the_base_score(sem: GradualSemantics) -> None:
    """An argument with no attackers or supporters keeps its intrinsic base score (stability)."""
    baf = BAF(arguments=frozenset({"a", "b"}))
    out = solve(baf, base={"a": 0.7, "b": 0.2}, semantics=sem)
    assert out.acceptability == pytest.approx({"a": 0.7, "b": 0.2})
    assert out.converged
    assert out.iterations == 1  # immediate fixpoint


@pytest.mark.parametrize("sem", BOTH, ids=lambda s: s.name)
def test_balance_equal_support_and_attack_leaves_the_base_unchanged(sem: GradualSemantics) -> None:
    """Equal aggregate support and attack cancel — the base score is returned (balance)."""
    baf = BAF(
        arguments=frozenset({"h", "p", "q"}),
        supports=(Edge("p", "h", 1.0),),
        attacks=(Edge("q", "h", 1.0),),
    )
    out = solve(baf, base={"h": 0.45, "p": 0.6, "q": 0.6}, semantics=sem)
    assert out.acceptability["h"] == pytest.approx(0.45)


@pytest.mark.parametrize("sem", BOTH, ids=lambda s: s.name)
def test_neutrality_zero_strength_edge_or_source_has_no_effect(sem: GradualSemantics) -> None:
    """An edge of strength 0, or from a strength-0 source, contributes nothing (neutrality)."""
    bare = BAF(arguments=frozenset({"h", "p"}), supports=(Edge("p", "h", 1.0),))
    base = {"h": 0.3, "p": 0.8}
    baseline = solve(bare, base=base, semantics=sem).acceptability["h"]

    zero_edge = BAF(
        arguments=frozenset({"h", "p", "z"}),
        supports=(Edge("p", "h", 1.0), Edge("z", "h", 0.0)),
    )
    zero_src = BAF(
        arguments=frozenset({"h", "p", "z"}),
        supports=(Edge("p", "h", 1.0), Edge("z", "h", 1.0)),
    )
    assert solve(zero_edge, base={**base, "z": 1.0}, semantics=sem).acceptability[
        "h"
    ] == pytest.approx(baseline)
    assert solve(zero_src, base={**base, "z": 0.0}, semantics=sem).acceptability[
        "h"
    ] == pytest.approx(baseline)


@pytest.mark.parametrize("sem", BOTH, ids=lambda s: s.name)
def test_monotonicity_support_raises_attack_lowers(sem: GradualSemantics) -> None:
    """Adding/strengthening support never decreases acceptability; adding attack never
    increases it (reinforcement/monotonicity)."""
    base = {"h": 0.4, "p1": 0.8, "p2": 0.8, "q": 0.8}
    one_supp = BAF(arguments=frozenset({"h", "p1"}), supports=(Edge("p1", "h", 0.7),))
    two_supp = BAF(
        arguments=frozenset({"h", "p1", "p2"}),
        supports=(Edge("p1", "h", 0.7), Edge("p2", "h", 0.7)),
    )
    with_attack = BAF(
        arguments=frozenset({"h", "p1", "q"}),
        supports=(Edge("p1", "h", 0.7),),
        attacks=(Edge("q", "h", 0.7),),
    )
    s1 = solve(one_supp, base=base, semantics=sem).acceptability["h"]
    s2 = solve(two_supp, base=base, semantics=sem).acceptability["h"]
    sa = solve(with_attack, base=base, semantics=sem).acceptability["h"]
    assert s2 >= s1  # more support does not lower
    assert sa <= s1  # an attacker does not raise


@pytest.mark.parametrize("sem", BOTH, ids=lambda s: s.name)
def test_boundedness_stays_in_unit_interval_under_piled_evidence(sem: GradualSemantics) -> None:
    """Acceptability stays in ``[0, 1]`` no matter how much support/attack is piled on."""
    supporters = [f"s{i}" for i in range(12)]
    attackers = [f"a{i}" for i in range(12)]
    baf = BAF(
        arguments=frozenset({"h", *supporters, *attackers}),
        supports=tuple(Edge(s, "h", 1.0) for s in supporters),
        attacks=tuple(Edge(a, "h", 1.0) for a in attackers),
    )
    base = {"h": 0.5, **dict.fromkeys(supporters, 1.0), **dict.fromkeys(attackers, 1.0)}
    out = solve(baf, base=base, semantics=sem)
    for v in out.acceptability.values():
        assert 0.0 <= v <= 1.0


@pytest.mark.parametrize("sem", BOTH, ids=lambda s: s.name)
def test_anonymity_edge_order_does_not_change_the_result(sem: GradualSemantics) -> None:
    """Aggregation is commutative, so permuting the edge order yields an identical result."""
    edges = (Edge("p1", "h", 0.3), Edge("p2", "h", 0.7), Edge("p3", "h", 0.5))
    base = {"h": 0.2, "p1": 0.9, "p2": 0.6, "p3": 0.8}
    forward = solve(
        BAF(frozenset({"h", "p1", "p2", "p3"}), supports=edges), base=base, semantics=sem
    )
    reversed_ = solve(
        BAF(frozenset({"h", "p1", "p2", "p3"}), supports=edges[::-1]), base=base, semantics=sem
    )
    assert forward.acceptability == pytest.approx(reversed_.acceptability)


@pytest.mark.parametrize("sem", BOTH, ids=lambda s: s.name)
def test_dangling_edge_is_ignored(sem: GradualSemantics) -> None:
    """An edge whose endpoint is not an argument lends/removes no strength (partial-tolerant)."""
    baf = BAF(
        arguments=frozenset({"h", "p"}),
        supports=(Edge("p", "h", 1.0), Edge("ghost", "h", 1.0), Edge("p", "missing", 1.0)),
    )
    out = solve(baf, base={"h": 0.3, "p": 0.8})
    only_real = solve(
        BAF(frozenset({"h", "p"}), supports=(Edge("p", "h", 1.0),)), base={"h": 0.3, "p": 0.8}
    )
    assert out.acceptability["h"] == pytest.approx(only_real.acceptability["h"])
