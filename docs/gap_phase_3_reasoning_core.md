# Gap Plan — Phase 3 (Reasoning Core: Two-Layer Propagation & Derivation)

**Why this file exists.** `todo_phase_3_reasoning_core.md` is the requirement list
(referencing `architecture.md` by §); this file is the **build plan** — the increment
breakdown (G3.x), the design decisions taken, and the sequencing — mirroring
`gap_phase_1_ingest.md` / `gap_phase_2_graph_construction.md`. `architecture.md` (§12 the
two-layer model, §6 the operators, §7.1 edge confidence) remains the source of truth for
every design decision.

**Depends on:** Phase 2 (reasoning nodes, `DERIVED_FROM` targets, both annotations present
on every node). The *pure* Layer A increments (G3.1/G3.2) depend on nothing but the
abstract derivation-graph contract; everything that touches real data depends on the
**Phase 2 adapter** (G3.4), which is therefore on the critical path for Layer B and the
operators.

## Build order (definition → in-memory incremental → wire to data → confidence → operators)

The novel core, built the `todo.md` way: a correct *definition* first, then the
incremental machinery diff-tested against it, then the data seam, then the second layer.
Correctness of Layer A is a **must-pass deterministic gate** (§12, §13), not a
tune-to-fit one — so the recompute oracle is built first precisely to be the test target
for every faster engine that follows.

| ID | Increment | Depends on | State |
|----|-----------|------------|-------|
| **G3.1** | **Layer A definitional core** — `well_founded_support` least-fixpoint over an abstract `DerivationGraph`; `RecomputeOracle` / `SupportOracle` contract; §12 must-pass cycle tests | Phase 2 (contract only) | shipped |
| **G3.2** | **Layer A incremental engine** — `IncrementalOracle`: Counting (integer support-count) + semi-naive insertion + **DRed** retraction; correct on acyclic *and* cyclic positive-Horn graphs; randomized diff-test vs `RecomputeOracle` | G3.1 | **shipped (this increment)** |
| G3.3 | **Cyclic/recursive completeness** — **clingo/ASP** foundedness for non-monotonic / stratified-negation rules; SCC detection to scope DRed over-deletion (perf); persisted `WITH RECURSIVE` / DBSP path | G3.2, G3.4 | planned |
| G3.4 | **Phase 2 adapter** — select the *active* subgraph (`valid_to` null, active boxes, `SAME_AS`-canonicalized components) and map AGE/UUID ids ↔ `NodeId`; feed Layer A | Phase 2, G2.3 | planned |
| **G3.5** | **Layer B semiring decision** — the Phase-3-entry fixture (deep vs shallow chain, multi-path) deciding **Viterbi `max-·` vs Gödel `max-min`** *before* any Layer B code (§12, review A6) | — | **shipped (this increment)** |
| **G3.6** | **Layer B confidence valuation** — least fixpoint over the chosen semiring, computed only over Layer-A-certified nodes; cycle-convergent (incremental-on-delta deferred) | G3.5, G3.2 | **shipped (this increment)** |
| G3.7 | **`SAME_AS`-component aggregation** — support/confidence accrue to the canonical component; merge/split is a belief-revision trigger re-running A/B on the affected component (§5.2) | G3.2, G3.6, G2.3 | planned |
| G3.8 | **Derivation operators** — `deduce` (→ `DeductiveConclusion`) and `induce` (→ provisional `InductiveConclusion`), `DERIVED_FROM` + provenance, each emitting an `Action` (§6, §10.2) | G3.4 | planned |
| G3.9 | **Composed-loop termination** — iteration bound + oscillation detection on REFUTES→retract→A→B→QBAF; non-convergence surfaced as a finding (§12, §7.2) | Phase 4 | planned |

Cross-cutting: every implementation of `SupportOracle` is **diff-tested against
`RecomputeOracle`** — the oracle is the contract. Layer A answers *membership* and
*multiplicity* only; Layer B answers *strength* only; the two annotations are never
merged (§12).

## G3.1 — Layer A definitional core (shipped earlier)

`core/truth_maintenance.py`: `well_founded_support(graph)` is the **definitional**
least-fixpoint grounded in `base_facts` (and empty-body axiomatic rules) and closed under
`Derivation`s, evaluated semi-naively. Pure / in-memory. Correct on cycles by
construction — a monotone positive program's least fixpoint *is* its well-founded model,
so an unfounded cycle is simply never reached from the base. `RecomputeOracle` wraps it as
the reference `SupportOracle`. The §12 must-pass tests (ungrounded cycle drops, grounded
cycle kept, one-of-several exactness) are in `tests/unit/test_truth_maintenance.py`.

## G3.2 — Layer A incremental engine (this increment)

**What shipped.** `IncrementalOracle` in `core/truth_maintenance.py`: a stateful
`SupportOracle` that maintains the well-founded support set across successive
`DerivationGraph` snapshots, doing work proportional to the *change* rather than the
graph. `apply(graph)` (and the `well_founded_support` alias) diffs the new snapshot
against the retained one and updates incrementally; `support_count(node)` exposes the
Layer A multiplicity.

**Design decisions taken up front (both the architecturally-load-bearing ones):**

- **Counting discipline = the §12 implementation of the fixpoint.** Each node carries an
  integer support-count (# active groundings: 1 if a base fact, +1 per derivation whose
  whole body is supported); supported ⇔ count > 0. Each derivation tracks `unmet` (body
  antecedents not yet supported) and fires when it hits 0. This is the additive,
  group-valued side of the §12 split — deliberately *not* confidence (which must be
  idempotent; that is Layer B).

- **DRed for retraction — the one decision that makes cycles correct.** Plain
  count-decrement deletion is *wrong* on a cycle: members keep each other's counts
  positive after their external grounding is gone (the classic unfounded-set bug, §12,
  §13). So deletion is **DRed (Delete–Rederive)**: (1) over-delete every currently-
  supported node reachable forward (body→head) from the removed grounding — tearing down
  an ungrounded cycle whole; (2) re-derive, from the surviving support and still-present
  base facts, only what genuinely re-grounds. A node held up solely by the broken cycle
  finds no seed and stays out. **This is correct on acyclic *and* cyclic positive-Horn
  graphs**, so it subsumes what the original plan split between "G3.2 acyclic Counting" and
  "G3.3 cyclic DRed" for the positive-Horn fragment — a single deletion algorithm correct
  everywhere, rather than count-decrement plus a fragile cycle-detection fallback. The
  genuinely separate G3.3 remainder is **non-monotonic/negation** foundedness (clingo —
  DRed's correctness assumes monotonicity) and SCC-scoping as a *performance* refinement.

**The subtle correctness point (documented as a regression guard).** A new derivation's
`unmet` is computed against a **frozen pre-batch baseline**, not the live mid-batch
support set. Otherwise a node that becomes supported *within* the same batch would be
counted as already-met at registration *and* delivered again by the cascade —
double-decrementing `unmet` below zero. The frozen baseline keeps every grounding counted
exactly once. (`test_incremental_insertion_handles_intra_batch_rule_chain`.)

**Tests (`tests/unit/test_truth_maintenance.py`, DB-free).** Protocol conformance;
single-snapshot and static-fixture equality with recompute; incremental insertion
(chain, base-fact unblocks a waiting rule, intra-batch chain); DRed retraction (sole vs
one-of-several support, diamond, removed *rule* not just base fact); the §12 cycle cases
incrementally (grounded kept → retracts fully; survives losing one of two groundings;
dropped cycle revived by a re-arriving base fact); `support_count` multiplicity; and the
headline gate — a **deterministic randomized diff-test** running one `IncrementalOracle`
through 40 seeds × 25 snapshots over a small node universe (so cycles, self-loops,
multi-grounded nodes and dangling antecedents all arise), asserting equality with a fresh
recompute after *every* step — plus a path-independence check. ruff + mypy(`src/iknos`)
clean.

**Deferred (documented seams, not regressions):**

- **Persisted incremental maintenance** (`WITH RECURSIVE` / IVM in Postgres, or DBSP at
  scale) — the in-memory engine is the algorithm; persisting it waits on the adapter
  (G3.4). The placement decision (in-Postgres vs alongside DBSP) is MVP-in-memory,
  revisited only if retraction latency misses SLA (§13).
- **clingo for non-monotonic / stratified negation** (G3.3) — this module is positive
  Horn only; negation/aggregation in rule bodies breaks the monotone-fixpoint =
  well-founded equivalence and must be routed to clingo.
- **SCC-scoped DRed** (G3.3) — DRed currently over-deletes the full forward-reachable set;
  scoping over-deletion to the affected SCC is a performance refinement, not a correctness
  one.

## G3.5 — Layer B semiring decision (this increment)

**The decision (recorded, eyes open): Gödel `max-min` is the Layer B default.** §12
mandates this be settled by a fixture *before* any valuation engine, because the choice is
epistemic — under Viterbi "confidence" partly measures derivation *depth*; under Gödel it
measures the *weakest link*. Switching later re-scores every conclusion and invalidates any
fitted §11.2 band thresholds, so it is decided first.

**What shipped.** `core/confidence.py` — the **algebra only**, not the engine (G3.6):

- **`Semiring`** — a frozen `(carrier=[0,1], ⊕=plus across alternative derivations,
  ⊗=times along a rule body, zero, one)`. Operations are stored as plain binary functions
  so distinct algebras are *values* (`VITERBI` and `GODEL` differ only in `times`), and the
  G3.6 engine selects one **at a seam** rather than branching on a kind. `combine_body` /
  `combine_alternatives` fold with the right identities (`one` for an empty/axiom body,
  `zero` for a node with no satisfied derivation).
- **`VITERBI = ([0,1], max, ·, 0, 1)`** and **`GODEL = ([0,1], max, min, 0, 1)`**;
  **`DEFAULT_SEMIRING = GODEL`**. Viterbi is *retained* (not deleted) for any future
  box whose degrees are genuinely probability-like rather than ordinal (§12's
  parenthetical) — the choice stays reversible at the seam.

**The fixture (`tests/unit/test_confidence_semiring.py`, DB-free), demonstrating the bias
numerically so the decision is not a default:**

- **Depth bias, headline case.** Certain base facts, every derivation step a 0.9-confidence
  `DERIVED_FROM` edge (§12's "five 0.9-confidence steps"). Deep chain (5 steps) vs shallow
  (1 step): **Viterbi** → deep `= 0.9**5 ≈ 0.590` *strictly weaker* than shallow `= 0.9`
  despite identical evidence quality (deep, careful derivation punished); **Gödel** → both
  `= 0.9`, depth-neutral. This divergence *is* the decision.
- **Weakest-link.** A chain with one weak (0.4) edge then strong (0.99) edges: Gödel pins
  the whole chain at `0.4` regardless of depth; Viterbi keeps eroding past the bottleneck.
- **Multi-path.** `⊕ = max` keeps the best derivation under *both* semirings (foundedness —
  *which* paths exist — is Layer A's job; Layer B only scores).
- **The laws G3.6's cyclic fixpoint relies on**, over a sample grid: identities,
  commutativity/associativity (`⊗` up to float rounding — products are not bit-exact
  associative), **`⊕` idempotence** (`a ⊕ a = a`) and **absorption** (`a ⊕ (a ⊗ b) = a`,
  since `a ⊗ b ≤ a` on `[0,1]`) — the two properties that make the confidence least fixpoint
  **converge on cyclic `DERIVED_FROM` graphs without inflation** — plus `[0,1]` closure and
  monotonicity (`⊗` never strengthens, `⊕` never weakens). The **sum-product** semiring is
  deliberately *not* offered (double-counts, diverges on cycles unless derivations are
  provably independent — §12).

ruff + mypy(`src/iknos`) clean; 311 unit tests pass.

**Consequence for G3.6.** The valuation engine is written once, generic over `Semiring`,
defaulting to `GODEL`. Because Gödel is depth-neutral, the §11.2 acceptability banding does
**not** need the depth-aware machinery a Viterbi choice would have forced.

**Deferred (the engine, not this decision):** the cycle-convergent confidence **least
fixpoint** over the chosen semiring — gated on Layer A's certified set, incremental on the
delta region Layer A reports — is **G3.6**. This increment is the algebra + the recorded
decision only.

## G3.6 — Layer B confidence valuation (this increment)

**What shipped.** `core/confidence.py::valuate` — the Layer B engine: the **least fixpoint**
of confidence over the chosen semiring, computed **only over the Layer-A-certified
`supported` set** handed in (the two-layer seam — Layer A decides *membership*, Layer B
scores it). Returns `{node: confidence}` for exactly the supported nodes. Generic over a
`Semiring`, defaulting to `GODEL` (the G3.5 decision), so Viterbi is a one-argument swap.

**The valuation.** A node's confidence is `⊕` (best derivation) over its grounds:
base-fact evidence confidence (`base_confidence[node]`, missing ⇒ `one`), and per
derivation `strength[d] ⊗ (⊗ body antecedents)` — the `DERIVED_FROM` edge strength (§7.1,
missing ⇒ `one`) times the body product. Confidences and edge strengths arrive as **side
maps keyed by the same `NodeId`/`Derivation` values Layer A uses** — Layer A's structures
are *not* mutated to carry strength (foundedness doesn't depend on it, and `Derivation`
equality keys the G3.2 diff), and the G3.4 adapter will populate both maps from AGE.

**Design decisions taken up front:**

- **Foundedness gates confidence (§12), structurally.** Only derivations whose head *and
  whole body* are in `supported` are indexed, so an **unfounded cycle — absent from
  `supported` — is never scored**, even though Layer B *would* converge on it. Membership,
  decided first by Layer A, is what keeps the cycle out; convergence is not foundedness.
- **Cycle-convergent by construction, not by tuning.** Kleene ascent (Jacobi iteration)
  from `zero`; `⊕` idempotent + the semiring absorptive (`a ⊕ (a ⊗ b) = a`) + ω-continuous
  ⇒ iterates are monotone, bounded by `one`, and reach the least fixpoint on acyclic **and**
  cyclic graphs — a grounded cycle *saturates* at its direct grounding instead of inflating.
  The loop is **bounded** (`len(supported)+2`, a safe ceiling since Jacobi propagates one
  edge/round and cycles add none); exceeding it — which an absorptive/ω-continuous `⊕`
  cannot — **raises rather than hangs**, the inner-layer analogue of §12's iteration bound.

**Tests (`tests/unit/test_confidence_valuation.py`, DB-free).** Conjunction (weakest link)
/ disjunction (best derivation) / base seeding / empty-body axiom; the §12 headline —
**unfounded cycle gets no confidence**, grounded cycle converges to its grounding; the G3.5
decision *through the real engine* (Gödel depth-neutral, Viterbi compounds); the two-layer
seam (scores exactly `well_founded_support`); and the strong gates — a **randomized
diff-test vs an independent memoized-recursion oracle on acyclic graphs** (60 seeds × both
semirings) and a **fixpoint-equation check on random cyclic graphs** (80 seeds × both
semirings, where tree enumeration is infinite but the fixpoint equation is a valid oracle).
ruff + mypy(`src/iknos`) clean; 325 unit tests pass.

**Deferred (documented seams, not regressions):**

- **Incremental-on-delta valuation** — §12 / the todo want Layer B recomputed only over the
  delta sub-graph Layer A reports as changed. This increment is the **definitional full
  recompute** (the Layer B analogue of G3.1's `RecomputeOracle`); the incremental engine
  (analogue of G3.2's `IncrementalOracle`) pairs with Layer A's delta reporting and the
  G3.4 wiring. The active subgraph is small per investigation, so full recompute is the
  correct MVP (§13: revisit only if latency misses SLA).
- **The adapter** that supplies real `base_confidence` (`EVIDENCED_BY` strength) and
  `strength` (`DERIVED_FROM` edge confidence) from AGE is **G3.4**; `valuate` is pure and
  takes them as arguments.
- **`SAME_AS`-component aggregation** (G3.7) — support/confidence accrue to the canonical
  component; a merge/split re-runs A/B on the affected component.

## Phase risks / decisions (carried from §12, §13)

- **Well-founded support is a correctness requirement, not a nicety.** Deterministic
  must-pass tests (grounded vs ungrounded cycle), not a tune-to-fit gate. G3.2's DRed +
  the diff-test discharge this for positive Horn; clingo discharges it under negation.
- **Viterbi's depth bias is a real epistemic choice** (§12, review A6): decide the Layer B
  semiring with the G3.5 fixture *before* Layer B code exists — switching later re-scores
  every conclusion and invalidates fitted band thresholds.
- **The two-layer split is our synthesis, not a packaged result** — validate the A→B seam
  carefully (§13). G3.1/G3.2 fix the A side of the seam (the `SupportOracle` contract +
  `support_count`); B is the open consumer.
