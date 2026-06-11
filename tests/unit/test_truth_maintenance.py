"""Unit tests for Layer A well-founded support — the definitional least-fixpoint (G3.1).

Pure: hand-built toy derivation graphs, no DB / AGE / LLM. Covers the §12 correctness
contract this layer exists to guarantee — *which* derived nodes are supported under
retraction:

- a conclusion gains support from base facts; retracting its sole support drops it;
- retracting **one of several** supports does *not* drop it (the exactness check);
- an **ungrounded** ``DERIVED_FROM`` cycle is unsupported and retracts fully when its
  external base support is removed; a **grounded** cycle (also reaching base) is kept;
- empty-body (axiomatic) rules ground their head;
- the result is deterministic and order-independent, and the semi-naive evaluation
  agrees with an obviously-correct naive fixpoint (guards the optimization and seeds the
  G3.2/G3.3 diff-test against this recompute oracle).
"""

import random
from collections.abc import Iterable

from iknos.core.truth_maintenance import (
    Derivation,
    DerivationGraph,
    IncrementalOracle,
    NodeId,
    RecomputeOracle,
    SupportOracle,
    well_founded_support,
)


def _graph(
    *,
    base: Iterable[NodeId] = (),
    rules: Iterable[tuple[NodeId, Iterable[NodeId]]] = (),
) -> DerivationGraph:
    """Build a graph from ``(conclusion, [antecedents])`` pairs — readable test fixtures."""
    return DerivationGraph(
        base_facts=frozenset(base),
        derivations=tuple(Derivation(conclusion=c, body=frozenset(body)) for c, body in rules),
    )


def _naive_support(graph: DerivationGraph) -> frozenset[NodeId]:
    """An obviously-correct (if inefficient) least-fixpoint oracle for the test to trust:
    repeatedly fire any rule whose whole body is supported until nothing changes. The
    semi-naive implementation under test must agree with this on every graph."""
    supported: set[NodeId] = set(graph.base_facts)
    changed = True
    while changed:
        changed = False
        for d in graph.derivations:
            if d.conclusion not in supported and d.body <= supported:
                supported.add(d.conclusion)
                changed = True
    return frozenset(supported)


# --- base facts & simple derivation ---


def test_base_fact_is_supported() -> None:
    assert well_founded_support(_graph(base=["A"])) == {"A"}


def test_empty_graph_supports_nothing() -> None:
    assert well_founded_support(_graph()) == frozenset()


def test_single_derivation_from_base_fact_is_supported() -> None:
    g = _graph(base=["A"], rules=[("C", ["A"])])
    assert well_founded_support(g) == {"A", "C"}


def test_conclusion_without_grounded_body_is_unsupported() -> None:
    # C rests on A, but A is not a base fact and nothing derives it.
    g = _graph(rules=[("C", ["A"])])
    assert well_founded_support(g) == frozenset()


def test_conjunctive_body_requires_all_antecedents() -> None:
    g = _graph(base=["A"], rules=[("C", ["A", "B"])])  # B missing
    assert well_founded_support(g) == {"A"}
    g2 = _graph(base=["A", "B"], rules=[("C", ["A", "B"])])
    assert well_founded_support(g2) == {"A", "B", "C"}


def test_derivation_chain_propagates() -> None:
    g = _graph(base=["A"], rules=[("C", ["A"]), ("D", ["C"])])
    assert well_founded_support(g) == {"A", "C", "D"}


# --- retraction: sole vs. one-of-several support ---


def test_retracting_sole_support_drops_conclusion() -> None:
    # Retraction in G3.1 = rebuild without the retracted base fact and recompute.
    supported = _graph(base=["A"], rules=[("C", ["A"])])
    retracted = _graph(base=[], rules=[("C", ["A"])])
    assert "C" in well_founded_support(supported)
    assert "C" not in well_founded_support(retracted)


def test_retracting_one_of_several_supports_keeps_conclusion() -> None:
    # C has two independent derivations; losing one leaves it supported (exactness check).
    both = _graph(base=["A", "B"], rules=[("C", ["A"]), ("C", ["B"])])
    one = _graph(base=["B"], rules=[("C", ["A"]), ("C", ["B"])])
    assert well_founded_support(both) == {"A", "B", "C"}
    assert "C" in well_founded_support(one)


def test_diamond_retraction_drops_everything_downstream() -> None:
    g = _graph(base=["A"], rules=[("B", ["A"]), ("C", ["A"]), ("D", ["B", "C"])])
    assert well_founded_support(g) == {"A", "B", "C", "D"}
    retracted = _graph(base=[], rules=[("B", ["A"]), ("C", ["A"]), ("D", ["B", "C"])])
    assert well_founded_support(retracted) == frozenset()


# --- cycles: the headline correctness requirement (§12) ---


def test_ungrounded_cycle_is_unsupported() -> None:
    # A <- B, B <- A, with no base grounding either: the unfounded-set case.
    g = _graph(rules=[("A", ["B"]), ("B", ["A"])])
    assert well_founded_support(g) == frozenset()


def test_grounded_cycle_is_kept() -> None:
    # A <- B, B <- A, but A also derives from base fact F: the whole cycle is well-founded.
    g = _graph(base=["F"], rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"])])
    assert well_founded_support(g) == {"F", "A", "B"}


def test_cycle_retracts_fully_when_external_base_support_removed() -> None:
    # Same cycle; remove F and the entire previously-grounded cycle must drop.
    retracted = _graph(base=[], rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"])])
    assert well_founded_support(retracted) == frozenset()


# --- axiomatic (empty-body) rules ---


def test_empty_body_rule_grounds_its_head() -> None:
    # An axiomatic domain rule (empty body) grounds its head with no base facts (§12).
    g = _graph(rules=[("AX", [])])
    assert well_founded_support(g) == {"AX"}


def test_empty_body_rule_feeds_downstream_derivations() -> None:
    g = _graph(rules=[("AX", []), ("C", ["AX"])])
    assert well_founded_support(g) == {"AX", "C"}


# --- robustness & determinism ---


def test_unknown_antecedent_is_tolerated_not_an_error() -> None:
    # A partial graph (antecedent neither base nor any head) yields an unsupported node,
    # never a crash — the layer tolerates the active-subgraph being incomplete.
    g = _graph(base=["A"], rules=[("C", ["A", "X"])])
    assert well_founded_support(g) == {"A"}


def test_result_is_order_independent_in_derivations() -> None:
    rules = [("D", ["C"]), ("C", ["B"]), ("B", ["A"])]
    forward = _graph(base=["A"], rules=rules)
    reversed_ = _graph(base=["A"], rules=list(reversed(rules)))
    assert well_founded_support(forward) == well_founded_support(reversed_)


def test_result_is_repeatable() -> None:
    g = _graph(base=["A", "B"], rules=[("C", ["A", "B"]), ("D", ["C"])])
    assert well_founded_support(g) == well_founded_support(g)


def test_node_both_base_fact_and_derived_conclusion() -> None:
    # A is a base fact and also the head of a rule whose body is unsupported: still
    # supported (its base grounding stands), no double-processing.
    g = _graph(base=["A"], rules=[("A", ["Z"])])
    assert well_founded_support(g) == {"A"}


# --- semi-naive agrees with the naive oracle (guards the optimization) ---


def test_semi_naive_matches_naive_oracle_on_assorted_graphs() -> None:
    graphs = [
        _graph(base=["A"], rules=[("C", ["A"]), ("D", ["C", "A"])]),
        _graph(base=["A", "B"], rules=[("C", ["A"]), ("C", ["B"]), ("E", ["C", "D"])]),
        _graph(base=["F"], rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"])]),
        _graph(rules=[("A", ["B"]), ("B", ["A"])]),
        _graph(rules=[("AX", []), ("C", ["AX"]), ("D", ["C", "missing"])]),
    ]
    for g in graphs:
        assert well_founded_support(g) == _naive_support(g)


# --- the recompute oracle (the diff-test target for G3.2/G3.3) ---


def test_recompute_oracle_matches_free_function() -> None:
    g = _graph(base=["A"], rules=[("C", ["A"]), ("D", ["C"])])
    assert RecomputeOracle().well_founded_support(g) == well_founded_support(g)


# ===========================================================================
# G3.2 — the incremental Counting + DRed oracle. The headline guarantee is that
# IncrementalOracle agrees with RecomputeOracle after *any* sequence of snapshots
# (the diff-test, below). The named cases document the specific behaviours.
# ===========================================================================


def test_incremental_oracle_satisfies_the_support_oracle_protocol() -> None:
    # Structural (runtime_checkable) — the contract Layer B will depend on.
    assert isinstance(IncrementalOracle(), SupportOracle)


def test_incremental_matches_recompute_on_single_snapshot() -> None:
    # One call on a fresh instance must equal a from-scratch recompute.
    g = _graph(base=["A", "B"], rules=[("C", ["A"]), ("C", ["B"]), ("E", ["C", "D"])])
    assert IncrementalOracle().well_founded_support(g) == well_founded_support(g)


def test_incremental_agrees_with_recompute_across_the_static_fixtures() -> None:
    graphs = [
        _graph(base=["A"], rules=[("C", ["A"]), ("D", ["C", "A"])]),
        _graph(base=["A", "B"], rules=[("C", ["A"]), ("C", ["B"]), ("E", ["C", "D"])]),
        _graph(base=["F"], rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"])]),
        _graph(rules=[("A", ["B"]), ("B", ["A"])]),
        _graph(rules=[("AX", []), ("C", ["AX"]), ("D", ["C", "missing"])]),
    ]
    for g in graphs:
        # A fresh oracle per graph — each is a single-snapshot equality check.
        assert IncrementalOracle().well_founded_support(g) == well_founded_support(g)


# --- incremental insertion ---


def test_incremental_insertion_propagates_a_chain() -> None:
    oracle = IncrementalOracle()
    assert oracle.apply(_graph(base=["A"])) == {"A"}
    # Adding the rules in a later snapshot must light up the whole chain.
    assert oracle.apply(_graph(base=["A"], rules=[("C", ["A"]), ("D", ["C"])])) == {"A", "C", "D"}


def test_incremental_insertion_of_base_fact_unblocks_a_waiting_rule() -> None:
    oracle = IncrementalOracle()
    g0 = _graph(base=["A"], rules=[("C", ["A", "B"])])  # B missing → C unsupported
    assert oracle.apply(g0) == {"A"}
    g1 = _graph(base=["A", "B"], rules=[("C", ["A", "B"])])  # B arrives → C fires
    assert oracle.apply(g1) == {"A", "B", "C"}


def test_incremental_insertion_handles_intra_batch_rule_chain() -> None:
    # Both rules added in one snapshot; E depends on C which is derived in the same batch.
    # Regression guard for the frozen-baseline unmet computation (no double-decrement).
    oracle = IncrementalOracle()
    g = _graph(base=["A"], rules=[("C", ["A"]), ("E", ["C"])])
    assert oracle.apply(g) == {"A", "C", "E"}


# --- incremental retraction (DRed) ---


def test_incremental_retracting_sole_support_drops_conclusion() -> None:
    oracle = IncrementalOracle()
    assert oracle.apply(_graph(base=["A"], rules=[("C", ["A"])])) == {"A", "C"}
    assert oracle.apply(_graph(base=[], rules=[("C", ["A"])])) == frozenset()


def test_incremental_retracting_one_of_several_supports_keeps_conclusion() -> None:
    oracle = IncrementalOracle()
    assert oracle.apply(_graph(base=["A", "B"], rules=[("C", ["A"]), ("C", ["B"])])) == {
        "A",
        "B",
        "C",
    }
    # Drop A; C still grounded through B.
    after = oracle.apply(_graph(base=["B"], rules=[("C", ["A"]), ("C", ["B"])]))
    assert "C" in after and "A" not in after


def test_incremental_diamond_retraction_drops_everything_downstream() -> None:
    oracle = IncrementalOracle()
    g = _graph(base=["A"], rules=[("B", ["A"]), ("C", ["A"]), ("D", ["B", "C"])])
    assert oracle.apply(g) == {"A", "B", "C", "D"}
    retracted = _graph(base=[], rules=[("B", ["A"]), ("C", ["A"]), ("D", ["B", "C"])])
    assert oracle.apply(retracted) == frozenset()


def test_incremental_retracting_a_derivation_not_just_a_base_fact() -> None:
    # DRed must also handle a removed *rule* (not only a removed base fact).
    oracle = IncrementalOracle()
    assert oracle.apply(_graph(base=["A"], rules=[("C", ["A"])])) == {"A", "C"}
    assert oracle.apply(_graph(base=["A"], rules=[])) == {"A"}  # rule gone → C drops


# --- the cycle correctness requirement, now incremental (§12) ---


def test_incremental_grounded_cycle_is_kept_then_retracts_fully() -> None:
    oracle = IncrementalOracle()
    grounded = _graph(base=["F"], rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"])])
    assert oracle.apply(grounded) == {"F", "A", "B"}
    # Remove the cycle's only external grounding: DRed must tear the whole cycle down,
    # not let its members hold each other up (the unfounded-set bug §12).
    retracted = _graph(base=[], rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"])])
    assert oracle.apply(retracted) == frozenset()


def test_incremental_cycle_survives_losing_one_of_two_groundings() -> None:
    oracle = IncrementalOracle()
    # Cycle A<->B grounded by both F (via A) and G (via B); drop F, G still holds it up.
    two = _graph(
        base=["F", "G"],
        rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"]), ("B", ["G"])],
    )
    assert oracle.apply(two) == {"F", "G", "A", "B"}
    one = _graph(base=["G"], rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"]), ("B", ["G"])])
    assert oracle.apply(one) == {"G", "A", "B"}


def test_incremental_re_grounding_revives_a_dropped_cycle() -> None:
    oracle = IncrementalOracle()
    rules = [("A", ["B"]), ("B", ["A"]), ("A", ["F"])]
    assert oracle.apply(_graph(base=[], rules=rules)) == frozenset()  # ungrounded → empty
    assert oracle.apply(_graph(base=["F"], rules=rules)) == {"F", "A", "B"}  # F arrives → revives


# --- support-count multiplicity (the "by how many derivations" question, §12) ---


def test_support_count_reflects_number_of_groundings() -> None:
    oracle = IncrementalOracle()
    oracle.apply(_graph(base=["A", "B"], rules=[("C", ["A"]), ("C", ["B"])]))
    assert oracle.support_count("C") == 2  # two independent derivations
    assert oracle.support_count("A") == 1  # base fact
    # Drop one derivation's support; C keeps one grounding.
    oracle.apply(_graph(base=["B"], rules=[("C", ["A"]), ("C", ["B"])]))
    assert oracle.support_count("C") == 1
    assert oracle.support_count("A") == 0  # unsupported


def test_support_count_zero_for_unsupported_node() -> None:
    oracle = IncrementalOracle()
    oracle.apply(_graph(rules=[("C", ["A"])]))  # A never grounded
    assert oracle.support_count("C") == 0
    assert oracle.support_count("never-seen") == 0


# --- the diff-test: random mutation sequences must track recompute exactly ---


def _random_graph(rng: random.Random, universe: list[NodeId]) -> DerivationGraph:
    """A random graph over a small node universe — bodies of size 0–3 drawn from the same
    universe, so cycles, self-loops, multi-grounded nodes and dangling antecedents all
    arise naturally. Small universe ⇒ dense overlap between successive snapshots ⇒ the
    diffs actually exercise incremental insertion *and* DRed retraction."""
    base = frozenset(n for n in universe if rng.random() < 0.35)
    rules: list[Derivation] = []
    for _ in range(rng.randint(0, len(universe) * 2)):
        conclusion = rng.choice(universe)
        body_size = rng.randint(0, 3)
        body = frozenset(rng.choice(universe) for _ in range(body_size))
        rules.append(Derivation(conclusion=conclusion, body=body))
    return DerivationGraph(base_facts=base, derivations=tuple(rules))


def test_incremental_matches_recompute_over_random_mutation_sequences() -> None:
    """The G3.2 correctness gate: one stateful :class:`IncrementalOracle` fed a long random
    sequence of snapshots must, after *every* step, report exactly what a fresh recompute
    of that snapshot reports — on acyclic and cyclic graphs alike. A single disagreement
    (a stuck unfounded cycle, a double-counted grounding, a missed re-derivation) fails the
    run, with the snapshot index for triage. Deterministic: fixed seeds, no wall-clock."""
    universe = ["A", "B", "C", "D", "E", "F"]
    for seed in range(40):
        rng = random.Random(seed)
        incremental = IncrementalOracle()
        for step in range(25):
            graph = _random_graph(rng, universe)
            got = incremental.apply(graph)
            expected = well_founded_support(graph)
            assert got == expected, f"seed={seed} step={step}: {got} != {expected}"


def test_two_incremental_instances_converge_regardless_of_path() -> None:
    """Path-independence: the support set depends only on the current graph, never on the
    sequence of edits that produced it. One oracle reaches the graph after a detour; a
    fresh oracle jumps straight to it; they must agree (and agree with recompute)."""
    final = _graph(
        base=["F", "G"],
        rules=[("A", ["B"]), ("B", ["A"]), ("A", ["F"]), ("C", ["A", "G"]), ("D", ["C"])],
    )
    detoured = IncrementalOracle()
    detoured.apply(_graph(base=["F"], rules=[("A", ["F"]), ("B", ["A"])]))
    detoured.apply(_graph(base=[], rules=[("A", ["B"]), ("B", ["A"])]))  # transient empty
    detoured.apply(_graph(base=["G"], rules=[("C", ["G"])]))
    assert detoured.apply(final) == IncrementalOracle().well_founded_support(final)
    assert detoured.apply(final) == well_founded_support(final)
