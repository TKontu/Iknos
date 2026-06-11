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

**Two implementations of the same definition.** :class:`RecomputeOracle` (G3.1) answers
every query by a full forward recompute — always correct, never incremental.
:class:`IncrementalOracle` (G3.2) maintains the support set across successive graph
snapshots with the §12 **Counting discipline** (a per-node integer support-count):
*insertions* propagate semi-naively forward (monotone — correct on cycles), and
*retractions* use **DRed (Delete–Rederive)** — over-delete everything reachable from the
removed grounding, then re-derive only what still re-grounds in surviving base facts.
DRed is precisely the cycle-safe deletion §12 mandates: it is correct on acyclic **and**
cyclic graphs, so it sidesteps the unfounded-set bug that plain count-decrement deletion
falls into on a cycle (an ungrounded cycle is over-deleted and never re-grounds, so it
never returns). Both oracles satisfy :class:`SupportOracle` and are held to the same
target: :class:`IncrementalOracle` is **diff-tested against** :class:`RecomputeOracle`
over arbitrary mutation sequences, including cyclic graphs (that is why recompute comes
first — it is the oracle, not the hot path).

Deliberately **pure**: no DB, no AGE, no LLM. It operates on an abstract derivation graph
and is unit-testable with hand-built toy graphs, exactly like ``core/consistency.py`` and
the scoring algebra in ``types/epistemic.py``.

Scope boundary — what is *not* yet here (so this is never mistaken for "Layer A done"):
  * **clingo / ASP** foundedness for **non-monotonic / stratified-negation** rules (G3.3):
    DRed below makes the *positive Horn* fragment — including cycles — incrementally
    correct, so the remaining cyclic work is specifically the negation case, plus SCC-
    scoped DRed as a performance refinement and the persisted ``WITH RECURSIVE`` path;
  * Layer B confidence (Viterbi / Gödel least fixpoint) — consumes the certified set here;
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

    G3.1 ships :class:`RecomputeOracle` (full recompute, correct on cycles); G3.2 ships
    :class:`IncrementalOracle` (Counting + DRed, correct on acyclic and cyclic positive
    Horn graphs). Every implementation satisfies this same contract and is **diff-tested
    against recompute**: after any sequence of changes, an incremental engine's support
    set must equal a fresh recompute of the resulting graph (§12).
    """

    def well_founded_support(self, graph: DerivationGraph) -> frozenset[NodeId]: ...


class RecomputeOracle:
    """The G3.1 reference implementation of :class:`SupportOracle`: recompute from
    scratch on every query. Always correct (acyclic and cyclic); not incremental. It is
    the oracle the later incremental engines are validated against, never the production
    hot path for large, slowly-changing graphs."""

    def well_founded_support(self, graph: DerivationGraph) -> frozenset[NodeId]:
        return well_founded_support(graph)


class IncrementalOracle:
    """G3.2 — the **incremental** :class:`SupportOracle`: maintain well-founded support
    across successive :class:`DerivationGraph` snapshots instead of recomputing each time.

    One stateful instance is fed a *sequence* of graphs (via :meth:`well_founded_support`
    or :meth:`apply`); each call diffs the new graph against the retained one and updates
    only the affected region. This is the production hot path G3.1's recompute oracle
    stands in for — same answer, work proportional to the change rather than the graph.

    **Counting discipline (§12).** Each node carries an integer ``support_count`` = the
    number of its *active groundings*: 1 if it is a base fact, plus 1 for every derivation
    whose entire body is currently supported. A node is supported iff its count is
    positive. Each derivation tracks ``unmet`` — how many of its body antecedents are not
    yet supported — and *fires* (contributes a grounding to its head) exactly when
    ``unmet`` reaches 0. Counting is what lets the layer answer "supported by **how many**
    derivations" (§6, §11.2), and it is the additive (group-valued) side of the §12 split
    — never confidence.

    **Insertion — semi-naive forward propagation.** Adding base facts / derivations only
    ever *adds* support, so a monotone forward pass from the newly-grounded nodes is
    correct on every graph (cycles included): when a node first becomes supported, the
    derivations that use it are revisited, firing as their last antecedent lands.

    **Retraction — DRed (Delete–Rederive), the cycle-safe deletion §12 requires.** Plain
    count-decrement is *wrong* on a cycle: the cycle's members keep each other's counts
    positive after their external grounding is gone (the unfounded-set bug). DRed avoids
    it in two phases: **(1) over-delete** — drop every currently-supported node reachable
    forward (body→head) from a removed grounding, so an ungrounded cycle is torn down
    whole; **(2) re-derive** — from the *surviving* support (and still-present base facts),
    run the same monotone forward pass to bring back only the over-deleted nodes that
    genuinely re-ground. A node supported only through the now-broken cycle has no surviving
    grounding, so it never returns. This is correct on acyclic and cyclic positive-Horn
    graphs alike; the G3.3 remainder is non-monotonic/negation foundedness (clingo) and
    SCC-scoped DRed as a perf refinement — not a correctness gap here.

    Pure and in-memory like the rest of the module; ids are opaque ``NodeId`` strings the
    Phase 2 adapter supplies. **Diff-tested against** :class:`RecomputeOracle` over random
    mutation sequences (``tests/unit/test_truth_maintenance.py``): the invariant is that
    after *any* sequence of snapshots this oracle's support set equals a fresh recompute.
    """

    def __init__(self) -> None:
        # Retained snapshot of the structure (for diffing the next graph against).
        self._base: set[NodeId] = set()
        self._derivs: dict[int, Derivation] = {}
        self._ids: dict[Derivation, int] = {}  # reverse map for diffing; dedupes by value
        self._next_id: int = 0
        # Live indices over the current structure.
        self._uses: dict[NodeId, set[int]] = defaultdict(set)  # node -> derivs using it
        self._heads: dict[NodeId, set[int]] = defaultdict(set)  # node -> derivs deriving it
        self._unmet: dict[int, int] = {}  # deriv id -> body antecedents not yet supported
        # The maintained result and its Counting state.
        self._support_count: defaultdict[NodeId, int] = defaultdict(int)
        self._supported: set[NodeId] = set()

    # -- public contract -----------------------------------------------------------

    def well_founded_support(self, graph: DerivationGraph) -> frozenset[NodeId]:
        """Apply ``graph`` as the next snapshot and return the current support set.

        Satisfies :class:`SupportOracle`. Because the instance is stateful, calling this
        with a *series* of graphs is the incremental path; calling it once on a fresh
        instance is equivalent to a recompute of that single graph.
        """
        return self.apply(graph)

    def apply(self, graph: DerivationGraph) -> frozenset[NodeId]:
        """Diff ``graph`` against the retained snapshot, update incrementally, return the
        support set. Deletions are processed before insertions so re-derivation runs
        against the post-deletion frontier and insertions then extend a correct state."""
        new_base = set(graph.base_facts)
        new_derivs = set(graph.derivations)

        removed_base = self._base - new_base
        added_base = new_base - self._base
        old_derivs = set(self._ids)
        removed_derivs = old_derivs - new_derivs
        added_derivs = new_derivs - old_derivs

        if removed_base or removed_derivs:
            self._delete(removed_base, removed_derivs)
        if added_base or added_derivs:
            self._insert(added_base, added_derivs)

        return frozenset(self._supported)

    def support_count(self, node: NodeId) -> int:
        """The number of active groundings of ``node`` (0 ⇔ unsupported). This is the
        Layer A multiplicity §12 keeps — e.g. a conclusion resting on a *single*
        derivation (count 1) is more fragile than one with several."""
        return self._support_count[node]

    # -- internals -----------------------------------------------------------------

    def _cascade(self, work: deque[NodeId]) -> None:
        """Monotone forward propagation shared by insertion and DRed re-derivation. Each
        node in ``work`` has just transitioned to supported; for every derivation that
        uses it, decrement ``unmet`` and, when a derivation's last antecedent lands, fire
        it — crediting its head a grounding and enqueueing the head if it is newly
        supported. Each (supported node, using-derivation) pair is touched once, so this
        terminates and is linear in the affected sub-graph."""
        while work:
            node = work.popleft()
            for i in self._uses.get(node, ()):
                self._unmet[i] -= 1
                if self._unmet[i] == 0:
                    head = self._derivs[i].conclusion
                    self._support_count[head] += 1
                    if head not in self._supported:
                        self._supported.add(head)
                        work.append(head)

    def _insert(self, added_base: set[NodeId], added_derivs: set[Derivation]) -> None:
        """Register added base facts and derivations, then propagate the new support
        forward in a single cascade.

        Each new derivation's ``unmet`` is computed against ``baseline`` — the support set
        *before* this batch, which is fully propagated by the inter-call invariant. The
        cascade then delivers every node that becomes supported **this** batch (added base
        facts and freshly-fired heads) exactly once. Computing ``unmet`` against the live,
        mid-batch ``_supported`` instead would let a node both be counted as already-met at
        registration *and* be delivered again by the cascade — double-decrementing
        ``unmet`` below zero. The frozen baseline is what keeps each grounding counted once.
        """
        baseline = frozenset(self._supported)
        work: deque[NodeId] = deque()

        for deriv in sorted(added_derivs, key=self._deriv_sort_key):
            i = self._next_id
            self._next_id += 1
            self._ids[deriv] = i
            self._derivs[i] = deriv
            self._heads[deriv.conclusion].add(i)
            for antecedent in deriv.body:
                self._uses[antecedent].add(i)
            self._unmet[i] = sum(1 for a in deriv.body if a not in baseline)

        for fact in sorted(added_base):
            self._base.add(fact)
            self._support_count[fact] += 1
            if fact not in self._supported:
                self._supported.add(fact)
                work.append(fact)

        # Derivations already satisfied by the baseline fire now (their antecedents are
        # stable, never re-delivered by the cascade); the rest fire inside the cascade as
        # this batch's new support reaches them.
        for deriv in sorted(added_derivs, key=self._deriv_sort_key):
            if self._unmet[self._ids[deriv]] == 0:
                self._support_count[deriv.conclusion] += 1
                if deriv.conclusion not in self._supported:
                    self._supported.add(deriv.conclusion)
                    work.append(deriv.conclusion)

        self._cascade(work)

    def _delete(self, removed_base: set[NodeId], removed_derivs: set[Derivation]) -> None:
        """DRed retraction. Unregister the removed groundings, over-delete everything that
        could rest on them, then re-derive whatever still re-grounds in surviving base
        facts."""
        # 1. Unregister removed structure; seed the over-deletion with the supported nodes
        #    that just lost a grounding (a removed base fact; the head of a firing removed
        #    derivation). A seed may still hold other groundings — DRed over-deletes it
        #    anyway and lets re-derivation decide, which is exactly what makes cycles safe.
        seeds: set[NodeId] = set()
        for fact in removed_base:
            self._base.discard(fact)
            if fact in self._supported:
                seeds.add(fact)
        for deriv in removed_derivs:
            i = self._ids.pop(deriv)
            was_firing = self._unmet.pop(i) == 0
            self._heads[deriv.conclusion].discard(i)
            for antecedent in deriv.body:
                self._uses[antecedent].discard(i)
            del self._derivs[i]
            if was_firing and deriv.conclusion in self._supported:
                seeds.add(deriv.conclusion)

        # 2. Over-delete: the forward (body→head) closure of the seeds within the currently
        #    supported set, over the *surviving* derivations. Removed derivations are gone
        #    from the indices, so they cannot carry the closure.
        over_deleted: set[NodeId] = set()
        stack = [n for n in seeds if n in self._supported]
        while stack:
            node = stack.pop()
            if node in over_deleted:
                continue
            over_deleted.add(node)
            for i in self._uses.get(node, ()):
                head = self._derivs[i].conclusion
                if head in self._supported and head not in over_deleted:
                    stack.append(head)

        # 3. Tentatively unsupport the over-deleted set and zero its counts (re-derivation
        #    rebuilds them). Recompute `unmet` for derivations whose body touches the set —
        #    their antecedent support just changed — so the re-derivation pass starts from a
        #    consistent frontier.
        for node in over_deleted:
            self._supported.discard(node)
            self._support_count[node] = 0
        affected: set[int] = set()
        for node in over_deleted:
            affected |= self._uses.get(node, set())
        for i in affected:
            body = self._derivs[i].body
            self._unmet[i] = sum(1 for a in body if a not in self._supported)

        # 4. Re-derive: seed with over-deleted nodes that still ground directly — a
        #    surviving base fact, or a derivation whose body is fully met by the survivors —
        #    then cascade. Nodes held up only by the torn-down cycle find no seed and stay
        #    out; their counts remain 0.
        work: deque[NodeId] = deque()
        for node in sorted(over_deleted):
            if node in self._base:
                self._support_count[node] += 1
                if node not in self._supported:
                    self._supported.add(node)
                    work.append(node)
            for i in self._heads.get(node, ()):
                if self._unmet[i] == 0:
                    self._support_count[node] += 1
                    if node not in self._supported:
                        self._supported.add(node)
                        work.append(node)
        self._cascade(work)

    @staticmethod
    def _deriv_sort_key(deriv: Derivation) -> tuple[NodeId, tuple[NodeId, ...]]:
        """Deterministic ordering for processing a batch of derivations, so the evaluation
        trace is replay-stable (§10) regardless of set iteration order."""
        return (deriv.conclusion, tuple(sorted(deriv.body)))
