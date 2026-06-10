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

from collections.abc import Iterable

from iknos.core.truth_maintenance import (
    Derivation,
    DerivationGraph,
    NodeId,
    RecomputeOracle,
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
