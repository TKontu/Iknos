"""G3.4 — unit tests for the Phase-2 adapter's pure assembly (DB-free).

The DB read methods of :class:`DerivationGraphAdapter` are exercised by the integration
test against real AGE; here we pin the **grouping / filtering** logic of
:func:`assemble_subgraph` — regrouping ``DERIVED_FROM`` edges into derivation bodies,
gating on the active-box set, seeding ``base_confidence``/``strength`` — with hand-built
rows, exactly as the truth-maintenance and confidence unit tests build toy graphs.
"""

from iknos.core.confidence import GODEL, VITERBI
from iknos.core.derivation_adapter import (
    ActiveSubgraph,
    DerivedRow,
    NodeRow,
    assemble_subgraph,
    support_and_confidence,
)
from iknos.core.truth_maintenance import Derivation


def _nodes(*specs: tuple[str, str | None, float]) -> list[NodeRow]:
    return [NodeRow(id=i, box=b, confidence=c) for i, b, c in specs]


def test_base_facts_are_the_active_evidenced_nodes() -> None:
    sub = assemble_subgraph(
        _nodes(("A", "bx", 0.9), ("B", "bx", 0.8)),
        base_fact_ids=["A", "B"],
        derived=[],
    )
    assert sub.graph.base_facts == frozenset({"A", "B"})
    assert sub.graph.derivations == ()
    assert sub.base_confidence == {"A": 0.9, "B": 0.8}


def test_derived_rows_regroup_into_one_conjunctive_body() -> None:
    # One derivation (group g1) with a two-node body A,B -> C.
    sub = assemble_subgraph(
        _nodes(("A", "bx", 1.0), ("B", "bx", 1.0), ("C", "bx", 1.0)),
        base_fact_ids=["A", "B"],
        derived=[
            DerivedRow("C", "A", "g1", 0.7),
            DerivedRow("C", "B", "g1", 0.7),
        ],
    )
    assert sub.graph.derivations == (Derivation(conclusion="C", body=frozenset({"A", "B"})),)
    assert sub.strength == {Derivation("C", frozenset({"A", "B"})): 0.7}


def test_distinct_groups_are_distinct_derivations_a_disjunction() -> None:
    # Two rule firings for the same conclusion C: {A} and {B} — a disjunction, two
    # Derivations, not one body {A,B}.
    sub = assemble_subgraph(
        _nodes(("A", "bx", 1.0), ("B", "bx", 1.0), ("C", "bx", 1.0)),
        base_fact_ids=["A", "B"],
        derived=[
            DerivedRow("C", "A", "g1", 0.6),
            DerivedRow("C", "B", "g2", 0.9),
        ],
    )
    assert set(sub.graph.derivations) == {
        Derivation("C", frozenset({"A"})),
        Derivation("C", frozenset({"B"})),
    }
    assert sub.strength[Derivation("C", frozenset({"A"}))] == 0.6
    assert sub.strength[Derivation("C", frozenset({"B"}))] == 0.9


def test_null_group_id_falls_back_to_grouping_by_conclusion() -> None:
    # Loose edges with no group-id read as a single conjunctive body per conclusion.
    sub = assemble_subgraph(
        _nodes(("A", "bx", 1.0), ("B", "bx", 1.0), ("C", "bx", 1.0)),
        base_fact_ids=["A", "B"],
        derived=[
            DerivedRow("C", "A", None, 0.5),
            DerivedRow("C", "B", None, 0.5),
        ],
    )
    assert sub.graph.derivations == (Derivation("C", frozenset({"A", "B"})),)


def test_inactive_box_excludes_nodes_and_starves_dependent_derivation() -> None:
    # B is in a deprecated box -> not active. The derivation A,B -> C keeps B in its body,
    # so C is unsupported (B never grounds). A stays a base fact.
    sub = assemble_subgraph(
        _nodes(("A", "active", 1.0), ("B", "dead", 1.0), ("C", "active", 1.0)),
        base_fact_ids=["A", "B"],
        derived=[
            DerivedRow("C", "A", "g1", 0.7),
            DerivedRow("C", "B", "g1", 0.7),
        ],
        active_box_ids=frozenset({"active"}),
    )
    assert sub.graph.base_facts == frozenset({"A"})  # B excluded
    # The derivation is kept (its conclusion C is active) but its body still names B.
    assert sub.graph.derivations == (Derivation("C", frozenset({"A", "B"})),)
    supported, _ = support_and_confidence(sub)
    assert supported == frozenset({"A"})  # C cannot fire — B is not active


def test_derivation_with_inactive_conclusion_is_dropped() -> None:
    sub = assemble_subgraph(
        _nodes(("A", "active", 1.0), ("C", "dead", 1.0)),
        base_fact_ids=["A"],
        derived=[DerivedRow("C", "A", "g1", 0.7)],
        active_box_ids=frozenset({"active"}),
    )
    assert sub.graph.derivations == ()
    assert sub.graph.base_facts == frozenset({"A"})


def test_none_active_box_filter_keeps_everything() -> None:
    sub = assemble_subgraph(
        _nodes(("A", "anything", 1.0), ("C", None, 1.0)),
        base_fact_ids=["A"],
        derived=[DerivedRow("C", "A", "g1", 0.7)],
        active_box_ids=None,
    )
    assert sub.graph.base_facts == frozenset({"A"})
    assert sub.graph.derivations == (Derivation("C", frozenset({"A"})),)


def test_antecedent_not_a_loaded_node_is_simply_unsupported() -> None:
    # 'X' is referenced by a DERIVED_FROM body but is not a loaded reasoning node (e.g. it
    # was retracted). The graph tolerates the partial reference; C never fires.
    sub = assemble_subgraph(
        _nodes(("A", "bx", 1.0), ("C", "bx", 1.0)),
        base_fact_ids=["A"],
        derived=[
            DerivedRow("C", "A", "g1", 0.7),
            DerivedRow("C", "X", "g1", 0.7),
        ],
    )
    supported, _ = support_and_confidence(sub)
    assert supported == frozenset({"A"})


def test_divergent_group_strength_takes_the_conservative_minimum() -> None:
    sub = assemble_subgraph(
        _nodes(("A", "bx", 1.0), ("C", "bx", 1.0)),
        base_fact_ids=["A"],
        derived=[
            DerivedRow("C", "A", "g1", 0.9),
            DerivedRow("C", "A", "g1", 0.4),  # stray disagreement
        ],
        active_box_ids=None,
    )
    assert sub.strength[Derivation("C", frozenset({"A"}))] == 0.4


def test_support_and_confidence_runs_the_two_layer_seam() -> None:
    # A(0.9) -> [strength 0.8] -> C. Gödel weakest-link: conf(C) = min(0.8, 0.9) = 0.8.
    sub = assemble_subgraph(
        _nodes(("A", "bx", 0.9), ("C", "bx", 1.0)),
        base_fact_ids=["A"],
        derived=[DerivedRow("C", "A", "g1", 0.8)],
    )
    supported, conf = support_and_confidence(sub, semiring=GODEL)
    assert supported == frozenset({"A", "C"})
    assert conf == {"A": 0.9, "C": 0.8}
    # Viterbi multiplies: conf(C) = 0.8 * 0.9 = 0.72.
    _, conf_v = support_and_confidence(sub, semiring=VITERBI)
    assert abs(conf_v["C"] - 0.72) < 1e-9


def test_empty_graph_assembles_cleanly() -> None:
    sub = assemble_subgraph([], [], [])
    assert sub == ActiveSubgraph(graph=sub.graph, base_confidence={}, strength={})
    assert sub.graph.base_facts == frozenset()
    assert sub.graph.derivations == ()
    supported, conf = support_and_confidence(sub)
    assert supported == frozenset()
    assert conf == {}


def test_derivations_are_deterministically_ordered() -> None:
    # Same rows in two orders must yield the same derivation tuple (replay stability, §10).
    rows = [
        DerivedRow("Z", "A", "g1", 0.5),
        DerivedRow("Y", "B", "g2", 0.5),
        DerivedRow("Z", "B", "g1", 0.5),
    ]
    nodes = _nodes(("A", "b", 1.0), ("B", "b", 1.0), ("Y", "b", 1.0), ("Z", "b", 1.0))
    a = assemble_subgraph(nodes, ["A", "B"], rows)
    b = assemble_subgraph(nodes, ["A", "B"], list(reversed(rows)))
    assert a.graph.derivations == b.graph.derivations
