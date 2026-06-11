"""G3.6 — Layer B confidence valuation: the foundedness-gated least fixpoint (§12).

Pure, DB-free, hand-built derivation graphs (the Layer A structures) + an independent
oracle. Covers the §12 / exit-criteria contract Layer B exists to guarantee:

- a node's confidence is ``⊕`` (best derivation) of ``strength ⊗ (⊗ body antecedents)``,
  seeded by base-fact evidence confidence — conjunction along a body, disjunction across
  derivations;
- **foundedness gates confidence**: only Layer-A-certified nodes are scored, so an
  **unfounded cycle never receives a confidence** even though Layer B would converge on it;
- a **grounded cycle converges** to the right value (absorption ⇒ saturates, no inflation);
- the chosen default (Gödel) is depth-neutral end-to-end; Viterbi compounds (the G3.5
  decision, now through the real engine);
- the headline correctness gate: a **randomized diff-test vs an independent recursive
  oracle** on acyclic graphs, and a **fixpoint-equation check** on cyclic graphs — for both
  semirings.
"""

import random
from collections.abc import Iterable, Mapping

from iknos.core.confidence import (
    DEFAULT_SEMIRING,
    GODEL,
    VITERBI,
    Confidence,
    Semiring,
    valuate,
)
from iknos.core.truth_maintenance import (
    Derivation,
    DerivationGraph,
    NodeId,
    well_founded_support,
)


def _graph(
    *,
    base: Iterable[NodeId] = (),
    rules: Iterable[tuple[NodeId, Iterable[NodeId]]] = (),
) -> DerivationGraph:
    return DerivationGraph(
        base_facts=frozenset(base),
        derivations=tuple(Derivation(conclusion=c, body=frozenset(b)) for c, b in rules),
    )


def _rule(conclusion: NodeId, body: Iterable[NodeId]) -> Derivation:
    return Derivation(conclusion=conclusion, body=frozenset(body))


def _recursive_oracle(
    graph: DerivationGraph,
    supported: frozenset[NodeId],
    base_confidence: Mapping[NodeId, Confidence],
    strength: Mapping[Derivation, Confidence],
    semiring: Semiring,
) -> dict[NodeId, Confidence]:
    """An independent, obviously-correct valuation for **acyclic** graphs: confidence is the
    best derivation tree, computed by memoized recursion. On a DAG this is exactly the least
    fixpoint :func:`valuate` must reach, by a wholly different evaluation strategy."""
    by_head: dict[NodeId, list[Derivation]] = {}
    for d in graph.derivations:
        by_head.setdefault(d.conclusion, []).append(d)
    memo: dict[NodeId, Confidence] = {}

    def conf(node: NodeId) -> Confidence:
        if node not in memo:
            alts: list[Confidence] = []
            if node in graph.base_facts:
                alts.append(base_confidence.get(node, semiring.one))
            for d in by_head.get(node, ()):
                if all(a in supported for a in d.body):
                    body = semiring.combine_body(conf(a) for a in d.body)
                    alts.append(semiring.times(strength.get(d, semiring.one), body))
            memo[node] = semiring.combine_alternatives(alts)
        return memo[node]

    return {n: conf(n) for n in supported}


def _satisfies_fixpoint(
    graph: DerivationGraph,
    supported: frozenset[NodeId],
    conf: Mapping[NodeId, Confidence],
    base_confidence: Mapping[NodeId, Confidence],
    strength: Mapping[Derivation, Confidence],
    semiring: Semiring,
) -> bool:
    """Independently restate the fixpoint equation and check ``conf`` satisfies it — a valid
    oracle on **cyclic** graphs too (where tree enumeration is infinite)."""
    for node in supported:
        alts: list[Confidence] = []
        if node in graph.base_facts:
            alts.append(base_confidence.get(node, semiring.one))
        for d in graph.derivations:
            if d.conclusion == node and all(a in supported for a in d.body):
                body = semiring.combine_body(conf[a] for a in d.body)
                alts.append(semiring.times(strength.get(d, semiring.one), body))
        if conf[node] != semiring.combine_alternatives(alts):
            return False
    return True


# --- conjunction / disjunction / base seeding -----------------------------------------


def test_base_fact_takes_its_evidence_confidence() -> None:
    graph = _graph(base=["a"])
    conf = valuate(graph, well_founded_support(graph), base_confidence={"a": 0.7})
    assert conf == {"a": 0.7}


def test_base_fact_defaults_to_certain_when_unmapped() -> None:
    graph = _graph(base=["a"])
    conf = valuate(graph, well_founded_support(graph))  # no base_confidence supplied
    assert conf == {"a": 1.0}  # missing ⇒ semiring.one (a certain leaf)


def test_conjunction_along_a_body_is_the_weakest_link_under_godel() -> None:
    graph = _graph(base=["a", "b"], rules=[("h", ["a", "b"])])
    d = _rule("h", ["a", "b"])
    conf = valuate(
        graph,
        well_founded_support(graph),
        base_confidence={"a": 0.9, "b": 0.6},
        strength={d: 0.8},
    )
    # Gödel: min(edge 0.8, min(a 0.9, b 0.6)) = 0.6.
    assert conf["h"] == 0.6


def test_disjunction_across_derivations_takes_the_best() -> None:
    graph = _graph(base=["a", "b"], rules=[("h", ["a"]), ("h", ["b"])])
    da, db = _rule("h", ["a"]), _rule("h", ["b"])
    conf = valuate(
        graph,
        well_founded_support(graph),
        base_confidence={"a": 0.3, "b": 0.85},
        strength={da: 1.0, db: 1.0},
    )
    assert conf["h"] == 0.85  # max across the two grounds


def test_empty_body_axiom_is_certain() -> None:
    graph = _graph(rules=[("axiom", [])])  # empty-body derivation = axiomatic rule
    conf = valuate(graph, well_founded_support(graph))
    assert conf["axiom"] == 1.0  # combine_body(()) == one


# --- foundedness gates confidence (the §12 headline) ----------------------------------


def test_unsupported_nodes_are_not_scored() -> None:
    # `c` depends on `missing`, which is neither a base fact nor any rule's head.
    graph = _graph(base=["a"], rules=[("c", ["missing"])])
    conf = valuate(graph, well_founded_support(graph))
    assert set(conf) == {"a"}
    assert "c" not in conf


def test_unfounded_cycle_never_receives_a_confidence() -> None:
    """The §12 / exit-criteria guarantee: an ungrounded ``DERIVED_FROM`` cycle (x↔y, no base)
    is dropped by Layer A and so is never scored — even though Layer B *would* converge on
    it. Foundedness, decided first, is what keeps it out."""
    graph = _graph(rules=[("x", ["y"]), ("y", ["x"])])
    supported = well_founded_support(graph)
    assert supported == frozenset()  # Layer A drops the unfounded cycle
    conf = valuate(graph, supported)
    assert conf == {}  # ...so Layer B assigns it no confidence


def test_grounded_cycle_converges_to_its_external_grounding() -> None:
    """A mutual-support pair x↔y that *also* both ground in base fact ``g`` is kept by Layer
    A; Layer B converges (absorption ⇒ the cyclic path never beats the direct grounding) to
    the grounding's strength, not above it."""
    graph = _graph(base=["g"], rules=[("x", ["g"]), ("y", ["g"]), ("x", ["y"]), ("y", ["x"])])
    supported = well_founded_support(graph)
    assert supported == frozenset({"g", "x", "y"})
    conf = valuate(graph, supported, base_confidence={"g": 0.75})
    # Direct ground gives 0.75; the cycle can only re-present ≤0.75, so it saturates there.
    assert conf == {"g": 0.75, "x": 0.75, "y": 0.75}


# --- depth behaviour through the real engine (the G3.5 decision, end-to-end) -----------


def test_default_engine_is_depth_neutral_viterbi_compounds() -> None:
    # f0 (certain base) → f1 → … → f4, every step a 0.9 edge.
    rules = [(f"f{i + 1}", [f"f{i}"]) for i in range(4)]
    graph = _graph(base=["f0"], rules=rules)
    supported = well_founded_support(graph)
    derivs = {_rule(f"f{i + 1}", [f"f{i}"]): 0.9 for i in range(4)}

    godel = valuate(graph, supported, base_confidence={"f0": 1.0}, strength=derivs)
    viterbi = valuate(
        graph, supported, base_confidence={"f0": 1.0}, strength=derivs, semiring=VITERBI
    )
    assert godel["f4"] == 0.9  # depth-neutral default
    assert abs(viterbi["f4"] - 0.9**4) < 1e-12  # Viterbi compounds with depth


# --- two-layer seam ---------------------------------------------------------------------


def test_valuate_scores_exactly_the_layer_a_certified_set() -> None:
    graph = _graph(base=["a"], rules=[("b", ["a"]), ("c", ["b"]), ("d", ["nope"])])
    supported = well_founded_support(graph)
    conf = valuate(graph, supported)
    assert set(conf) == set(supported) == {"a", "b", "c"}


def test_idempotent_rerun_is_stable() -> None:
    graph = _graph(base=["a"], rules=[("b", ["a"]), ("c", ["a", "b"])])
    supported = well_founded_support(graph)
    once = valuate(graph, supported, base_confidence={"a": 0.8})
    twice = valuate(graph, supported, base_confidence={"a": 0.8})
    assert once == twice


# --- randomized diff-tests vs independent oracles (the strong gate) --------------------


def _random_dag(
    rng: random.Random, n: int
) -> tuple[DerivationGraph, dict[NodeId, Confidence], dict[Derivation, Confidence]]:
    """A random *acyclic* graph: a rule's body only references strictly-lower-indexed nodes,
    so cycles are impossible and the recursive oracle terminates."""
    names = [f"n{i}" for i in range(n)]
    base = frozenset(name for name in names if rng.random() < 0.4)
    derivs: list[Derivation] = []
    strength: dict[Derivation, Confidence] = {}
    for i in range(n):
        for _ in range(rng.randint(0, 2)):
            lowers = names[:i]
            body = frozenset(rng.sample(lowers, rng.randint(0, min(2, len(lowers)))))
            d = Derivation(conclusion=names[i], body=body)
            derivs.append(d)
            strength[d] = round(rng.uniform(0.1, 1.0), 3)
    graph = DerivationGraph(base_facts=base, derivations=tuple(derivs))
    base_conf = {name: round(rng.uniform(0.1, 1.0), 3) for name in base}
    return graph, base_conf, strength


def test_acyclic_valuation_matches_independent_recursive_oracle() -> None:
    for seed in range(60):
        rng = random.Random(seed)
        graph, base_conf, strength = _random_dag(rng, n=rng.randint(1, 9))
        supported = well_founded_support(graph)
        for semiring in (GODEL, VITERBI):
            got = valuate(
                graph, supported, base_confidence=base_conf, strength=strength, semiring=semiring
            )
            want = _recursive_oracle(graph, supported, base_conf, strength, semiring)
            assert got.keys() == want.keys()
            for node in got:
                assert abs(got[node] - want[node]) < 1e-9, (seed, semiring.name, node)


def _random_graph(
    rng: random.Random, n: int
) -> tuple[DerivationGraph, dict[NodeId, Confidence], dict[Derivation, Confidence]]:
    """A random graph with **no acyclicity constraint** — self-loops and cycles arise."""
    names = [f"n{i}" for i in range(n)]
    base = frozenset(name for name in names if rng.random() < 0.35)
    derivs: list[Derivation] = []
    strength: dict[Derivation, Confidence] = {}
    for i in range(n):
        for _ in range(rng.randint(0, 2)):
            body = frozenset(rng.sample(names, rng.randint(0, min(2, n))))
            d = Derivation(conclusion=names[i], body=body)
            derivs.append(d)
            strength[d] = round(rng.uniform(0.1, 1.0), 3)
    graph = DerivationGraph(base_facts=base, derivations=tuple(derivs))
    base_conf = {name: round(rng.uniform(0.1, 1.0), 3) for name in base}
    return graph, base_conf, strength


def test_cyclic_valuation_converges_to_a_gated_fixpoint() -> None:
    for seed in range(80):
        rng = random.Random(1000 + seed)
        graph, base_conf, strength = _random_graph(rng, n=rng.randint(1, 8))
        supported = well_founded_support(graph)
        for semiring in (GODEL, VITERBI):
            conf = valuate(
                graph, supported, base_confidence=base_conf, strength=strength, semiring=semiring
            )
            # scored exactly the founded set; every value a valid degree
            assert conf.keys() == set(supported)
            assert all(0.0 <= v <= 1.0 for v in conf.values())
            # and it is a genuine fixpoint of the (foundedness-gated) equation
            assert _satisfies_fixpoint(graph, supported, conf, base_conf, strength, semiring), (
                seed,
                semiring.name,
            )


def test_default_semiring_is_used_when_unspecified() -> None:
    graph = _graph(base=["a"], rules=[("b", ["a"])])
    supported = well_founded_support(graph)
    d = _rule("b", ["a"])
    explicit = valuate(
        graph, supported, base_confidence={"a": 0.5}, strength={d: 0.9}, semiring=DEFAULT_SEMIRING
    )
    implicit = valuate(graph, supported, base_confidence={"a": 0.5}, strength={d: 0.9})
    assert explicit == implicit
