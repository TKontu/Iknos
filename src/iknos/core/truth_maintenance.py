"""Layer A — truth maintenance: well-founded support (G3.1, architecture §12).

This is the correctness spine of the reasoning core. Layer A owns **retraction**: it
answers *which* derived nodes are supported, never *how strongly* (that is Layer B's
confidence valuation, computed only over what this layer certifies). The two annotations
are never collapsed (§12).

**Well-founded support is the definition.** A node is supported iff it is in the least
fixpoint built from **base facts** (grounded by ``EVIDENCED_BY``, or axiomatic domain
rules) and closed under derivations: *if every antecedent of some derivation is
supported, its conclusion is.* The per-node integer support-count Layer A carries is the
incremental *implementation* of this fixpoint (G3.2), not its definition.

**Why this founded semantics, computed by full recompute, is correct on cycles too.**
The whole point of the layer is to distinguish a grounded mutual-support pair (both also
reach base → kept) from an **unfounded cycle** (nodes that support only each other once
their external grounding is retracted → dropped). Because a derivation program with no
negation is *monotone*, its least fixpoint **is** the well-founded model, and computing
it forward from base facts is correct on acyclic and cyclic graphs alike: an unfounded
cycle is simply never reached from the base, so it never enters the set. The cycle
*problem* (the classic unfounded-set bug) is specific to **incremental** maintenance with
integer counts — a cycle keeps its own count positive after its grounding is removed.
G3.1 sidesteps it by recomputing; G3.2 adds incremental Counting for the acyclic
majority and G3.3 routes nontrivial ``DERIVED_FROM`` SCCs to DRed / clingo, each
**diff-tested against the recompute oracle here** (that is why recompute comes first).

Deliberately **pure**: no DB, no AGE, no LLM. It operates on an abstract derivation graph
and is unit-testable with hand-built toy graphs, exactly like ``core/consistency.py`` and
the scoring algebra in ``types/epistemic.py``.

Scope boundary — explicitly *not* in G3.1 (so this is never mistaken for "Layer A done"):
  * incremental integer support-count over acyclic regions (G3.2);
  * DRed (over-delete then re-derive) / clingo for incremental cyclic retraction (G3.3);
  * Layer B confidence (Viterbi least fixpoint) — consumes the certified set from here;
  * the Phase 2 adapter that selects the *active* subgraph (``valid_to`` null, active
    boxes, ``SAME_AS``-canonicalized components) and maps AGE/UUID ids into this graph;
  * **negation / aggregation in rule bodies** — that breaks the monotone least-fixpoint =
    well-founded equivalence and must be routed to clingo (stratified, §13). This module
    is positive Horn only.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# The pure layer is id-type-agnostic at the value level, but ids are modelled as `str`:
# in production these are AGE/UUID node ids the Phase 2 adapter stringifies at the
# boundary. Keeping them `str` makes the module decoupled, audit logs and replay traces
# readable, and toy fixtures legible (``"A"``, ``"B"``).
type NodeId = str


@dataclass(frozen=True)
class Derivation:
    """One derivation rule: ``conclusion`` holds if every node in ``body`` holds.

    ``body`` is a conjunction (all antecedents required); a conclusion supported by more
    than one rule is a disjunction across them — losing one derivation does not drop it
    while another still grounds it. An **empty** ``body`` is an axiomatic rule: it grounds
    its ``conclusion`` unconditionally, modelling the architecture's "axiomatic domain
    rules" grounding anchor alongside evidence-grounded base facts (§12).
    """

    conclusion: NodeId
    body: frozenset[NodeId] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DerivationGraph:
    """An immutable snapshot of the derivation structure to evaluate.

    ``base_facts`` are the grounding anchor (the ``EVIDENCED_BY`` leaves). ``derivations``
    are the rules. The graph tolerates being *partial*: an antecedent that is neither a
    base fact nor any rule's head is simply unsupported (and so are its dependents) — the
    active subgraph handed in by the Phase 2 adapter need not be closed.
    """

    base_facts: frozenset[NodeId] = field(default_factory=frozenset)
    derivations: tuple[Derivation, ...] = ()


def well_founded_support(graph: DerivationGraph) -> frozenset[NodeId]:
    """Return the well-founded support: the least fixpoint grounded in ``base_facts`` and
    closed under ``derivations`` (§12).

    Evaluated **semi-naively**: each rule tracks how many of its body antecedents are not
    yet supported; when a node becomes supported, only the rules that *use* it are
    revisited, and a rule fires the moment its last antecedent lands. This is the standard
    forward least-fixpoint evaluation — linear in the graph (each base fact and each
    body-edge processed once), not a repeated full re-scan — so it does not degrade on
    large active subgraphs in production.

    The returned set is order-independent; the worklist is seeded in a deterministic order
    so any future logging of the evaluation trace is replay-stable (§10). Correct on
    acyclic and cyclic graphs alike (see module docstring): an unfounded cycle is never
    reached from the base and so never enters the set.
    """
    # uses[a] = indices of derivations that have `a` as a body antecedent.
    uses: dict[NodeId, list[int]] = defaultdict(list)
    # remaining[i] = body antecedents of derivation i not yet known supported.
    remaining: list[int] = [len(d.body) for d in graph.derivations]
    for i, d in enumerate(graph.derivations):
        for antecedent in d.body:
            uses[antecedent].append(i)

    supported: set[NodeId] = set()
    worklist: deque[NodeId] = deque()

    def mark(node: NodeId) -> None:
        # Each node transitions to supported at most once, so each rule's `remaining`
        # is decremented exactly once per (distinct) supported body antecedent.
        if node not in supported:
            supported.add(node)
            worklist.append(node)

    # Seed: base facts (deterministic order), then axiomatic empty-body rules fire at once.
    for fact in sorted(graph.base_facts):
        mark(fact)
    for i, count in enumerate(remaining):
        if count == 0:
            mark(graph.derivations[i].conclusion)

    while worklist:
        node = worklist.popleft()
        for i in uses.get(node, ()):
            remaining[i] -= 1
            if remaining[i] == 0:
                mark(graph.derivations[i].conclusion)

    return frozenset(supported)


@runtime_checkable
class SupportOracle(Protocol):
    """The membership contract Layer A exposes upward (to Layer B) and the fixed target
    every implementation is checked against.

    G3.1 ships :class:`RecomputeOracle` (full recompute, correct on cycles). The
    incremental engines — G3.2 Counting over acyclic regions, G3.3 DRed / clingo over
    cyclic SCCs — satisfy this same contract and are **diff-tested against recompute**:
    after any sequence of changes, an incremental engine's support set must equal a fresh
    recompute of the resulting graph (§12).
    """

    def well_founded_support(self, graph: DerivationGraph) -> frozenset[NodeId]: ...


class RecomputeOracle:
    """The G3.1 reference implementation of :class:`SupportOracle`: recompute from
    scratch on every query. Always correct (acyclic and cyclic); not incremental. It is
    the oracle the later incremental engines are validated against, never the production
    hot path for large, slowly-changing graphs."""

    def well_founded_support(self, graph: DerivationGraph) -> frozenset[NodeId]:
        return well_founded_support(graph)
