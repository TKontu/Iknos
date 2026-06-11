# Phase 3 — Reasoning Core: Two-Layer Propagation & Derivation

**Goal:** the novel core. Maintain *which* derived nodes are supported under retraction
(Layer A) and *how strongly* (Layer B), and derive conclusions from facts. No
off-the-shelf system packages this — it is the substance of the project.

**Depends on:** Phase 2 (nodes, `DERIVED_FROM` targets, both annotations present).
**Architecture refs:** §12 (two-layer model), §8 (decisions, staged build 1–2), §7.1
(edge confidence), §6 (`deduce`, `induce`).

**Status — 🟡 G3.1 + G3.2 shipped (Layer A, in-memory); G3.5 + G3.6 shipped (Layer B
semiring decision + confidence valuation, in-memory).** Well-founded support is
implemented as the **definitional least-fixpoint** (`well_founded_support`, exposed as
`RecomputeOracle`) over an abstract derivation graph — pure, in-memory, correct on
acyclic *and* cyclic graphs — in `core/truth_maintenance.py`, with the §12 must-pass
correctness tests passing deterministically (**G3.1**). On top of it, **G3.2** ships the
*incremental* engine `IncrementalOracle`: the §12 **Counting discipline** (per-node
integer support-count) with semi-naive forward propagation for insertions and **DRed
(Delete–Rederive) for retractions**. DRed is the cycle-safe deletion §12 mandates, so the
incremental engine is correct on acyclic **and** cyclic *positive-Horn* graphs — verified
by a deterministic randomized **diff-test against `RecomputeOracle`** over long mutation
sequences (1000 snapshots), exactly as planned. What remains of Layer A (**G3.3**): the
**clingo/ASP** path for **non-monotonic / stratified-negation** rules, SCC-scoped DRed as
a performance refinement, and the *persisted* (`WITH RECURSIVE` / DBSP) path — all of
which need the **Phase 2 adapter** (active-subgraph selection + AGE/UUID→`DerivationGraph`
mapping), still open. Layer B is now in
memory too: the semiring is **decided (G3.5: Gödel `max-min` default)** and the
**foundedness-gated confidence least-fixpoint** ships (**G3.6: `core/confidence.py::valuate`**,
cycle-convergent; incremental-on-delta deferred). **G3.4** now wires both layers to real AGE
data: `core/derivation_adapter.py` reads the *active* subgraph (`valid_to` null, active
boxes) and assembles the `DerivationGraph` + Layer B side maps, defining the `DERIVED_FROM`
grouping contract the operators will write. **G3.8** ships those operators:
`core/derive.py` `deduce`/`induce` write `DeductiveConclusion`/`InductiveConclusion` nodes +
`DERIVED_FROM` groups + provenance + an `Action`, with both annotations **computed by Layer
A/B** ("engine disposes"). **G3.7** ships `SAME_AS`-component aggregation
(`core/component_aggregate.py`): support/confidence accrue to the canonical component
(additive Layer A, `⊕` Layer B), with merge/split as belief-revision triggers that
re-aggregate. **G3.9** ships the composed-loop **termination driver**
(`core/composed_loop.py::stabilize`): bounded iteration + oscillation detection that surfaces
a non-converging region as a finding (the Phase-4 `REFUTES→A→B→QBAF` loop body wires into it).
Still open: the clingo/SCC/persisted path (G3.3, needs a solver dep + the persisted layer).
See `gap_phase_3_reasoning_core.md` for the increment-by-increment build plan.

## Layer A — truth maintenance over a commutative group (owns retraction)

- [x] **Well-founded support is the definition:** a node is supported iff it is in the
      least fixpoint grounded in **base facts** (`EVIDENCED_BY` leaves / axiomatic rules)
      and closed under derivations (§12). Mark base facts explicitly as the grounding
      anchor. The integer support-count is the incremental *implementation*, not the
      definition. *(G3.1 — `core/truth_maintenance.py`: `DerivationGraph.base_facts` is
      the explicit grounding anchor, empty-body `Derivation`s model axiomatic rules; the
      integer support-count itself is deferred to the G3.2 incremental impl.)*
- [x] **Acyclic regions:** Counting over `DERIVED_FROM` (cheap, correct here);
      retraction propagates and a conclusion survives if support remains.
      *(G3.2 — `IncrementalOracle`: per-node integer support-count, semi-naive forward
      insertion, work proportional to the change. The **in-memory** incremental engine is
      done and diff-tested against recompute; the equivalent over the **persisted** graph
      (`WITH RECURSIVE` / IVM in Postgres) follows the Phase 2 adapter.)*
- [x] **Cyclic regions (positive Horn):** retraction is cycle-safe via **DRed
      (over-delete everything reachable from the removed grounding, then re-derive only
      what re-grounds in base facts)** — plain count-decrement is *not* correct here, and
      DRed is. *(G3.2 — `IncrementalOracle._delete`; the grounded/ungrounded cycle
      retraction and revival cases pass incrementally, and the randomized diff-test covers
      cyclic graphs.)* **Open (G3.3):** route **non-monotonic / stratified-negation**
      regions to **clingo** (ASP unfounded-set elimination — DRed's correctness assumes
      monotone positive Horn), plus **SCC detection** to scope DRed's over-deletion as a
      performance refinement, and the persisted/DBSP path. *(G3.1's recompute is the oracle
      for all of it; G3.2's DRed already settles the positive-Horn cyclic correctness.)*
- [x] **Correctness tests (must-pass, deterministic):** an *ungrounded* derivation cycle
      retracts fully when its external base support is removed; a *grounded* mutual-
      support pair (also reaching base) is correctly kept. *(G3.1 —
      `tests/unit/test_truth_maintenance.py`: `test_ungrounded_cycle_is_unsupported`,
      `test_cycle_retracts_fully_when_external_base_support_removed`,
      `test_grounded_cycle_is_kept`.)*
- [x] Exactness check: deletion of one of several supports does **not** drop a node.
      *(G3.1 — `test_retracting_one_of_several_supports_keeps_conclusion`.)*
- [ ] (Scale path noted, not built: Differential Dataflow / DBSP fed by Postgres CDC —
      recursive retraction correct by construction.)

## Layer B — confidence valuation over an absorptive semiring (owns strength)

- [x] **Semiring decision first (Phase-3-entry, before any Layer B code; §12, review
      A6).** Viterbi `max-·` has a structural **depth bias**: confidence decays
      geometrically with derivation depth (five 0.9 steps → 0.59 regardless of
      evidence quality), so deep derivations are punished and acceptability-band
      thresholds mean different things at different chain lengths. Gödel `max-min`
      is depth-neutral (weakest link). Build a small fixture — one deep chain vs one
      shallow chain from equal-confidence facts, plus a multi-path graph — compute
      both semirings over it, and decide with eyes open. If Viterbi is chosen, the
      §11.2 banding must be made depth-aware (note the extra machinery in the
      decision record). Both are absorptive/ω-continuous, so cycle convergence is
      unaffected either way. *(G3.5 — **decided: Gödel `max-min` is the Layer B
      default** (depth-neutral → no depth-aware banding needed). `core/confidence.py`
      ships the `Semiring` algebra + `VITERBI`/`GODEL`/`DEFAULT_SEMIRING`; the
      depth-bias fixture + semiring laws are in `tests/unit/test_confidence_semiring.py`.
      Viterbi retained as a value for future probability-like boxes. The valuation
      **engine** is G3.6.)*
- [x] Confidence as a **least fixpoint** over the chosen semiring — Viterbi
      `([0,1], max, ·, 0, 1)`: multiply along a rule body, max across alternative
      derivations (best-derivation confidence); or Gödel `max-min` (ordinal,
      depth-neutral). *(G3.6 — `core/confidence.py::valuate`, Kleene ascent, generic over
      `Semiring`, default `GODEL`.)*
- [x] Compute only over nodes Layer A certifies as **well-founded**-supported (so an
      unfounded cycle never receives a confidence — foundedness gates scoring). *(G3.6 —
      `valuate` indexes only derivations whose head **and** whole body are in the certified
      `supported` set; `test_unfounded_cycle_never_receives_a_confidence`.)*
- [ ] Recompute incrementally on the **delta-affected sub-graph** Layer A reports.
      *(Open. G3.6 ships the **definitional full recompute** — the Layer B analogue of
      G3.1's `RecomputeOracle`; the incremental engine pairs with Layer A delta reporting +
      the G3.4 adapter. Full recompute is the MVP (§13); revisit only if latency misses SLA.)*
- [x] Verify convergence on **cyclic** derivation graphs (absorptive + ω-continuous →
      saturates, no inflation). **Never** use the sum-product semiring here. *(G3.6 —
      grounded-cycle convergence test + an 80-seed fixpoint-equation check on random cyclic
      graphs; sum-product is deliberately not offered.)*

## Two-layer integration

- [x] Clean interface: Layer A decides membership → Layer B scores it. Two annotations,
      never merged (§12). *(Both sides now in place — G3.1 defines the `SupportOracle`
      contract returning the certified well-founded set; G3.2's `IncrementalOracle`
      satisfies it (and exposes `support_count`); **G3.6's `valuate` consumes that set** and
      returns the `[0,1]` confidence over exactly it (`base_confidence`/`strength` as side
      maps, never merged into the count). `test_valuate_scores_exactly_the_layer_a_certified_set`.)*
- [x] Confirm idempotent confidence is *not* subtracted; retraction lives only in the
      group/count layer. *(G3.6 — `valuate` is a pure fixpoint with no subtraction;
      retraction is entirely Layer A's DRed/count layer. `test_idempotent_rerun_is_stable`.)*
- [x] **Aggregate evidence over `SAME_AS` components (§5.2):** support counts and
      confidence accrue to the canonical component, not the raw node. A **merge/split**
      (assert/retract `SAME_AS`) is a belief-revision trigger — re-run Layer A/B over the
      affected component only. *(G3.7 — `core/component_aggregate.py`: `aggregate_components`
      folds per-node Layer A/B annotations to the canonical component — **additive** support
      (§12 counting), **`⊕`/max** confidence (idempotent, best evidence); `ComponentReasoner.merge`
      / `.split` are the belief-revision triggers (assert confirmed `SAME_AS` / bitemporally
      retract it) that re-aggregate and emit an Action, so over-merge is recoverable. The
      **affected-component-only** scoping is the deferred incremental refinement — MVP
      re-aggregates the whole active subgraph, §13.)*

## Derivation operators (§6)

- [x] `deduce`: facts/conclusions → `DeductiveConclusion`, with `DERIVED_FROM` edges
      and provenance to underlying facts' spans. *(G3.8 — `core/derive.py::Deriver.deduce`;
      premises may be Facts **or** prior conclusions (chaining), the `DERIVED_FROM` group
      honours the G3.4 contract, and provenance to source spans is both structural
      (`conclusion→DERIVED_FROM→premise→EVIDENCED_BY→Span`) and recorded on the Action.)*
- [x] `induce`: facts → `InductiveConclusion`, marked provisional. *(G3.8 —
      `Deriver.induce`; `provisional=True` on the node, required step `strength`.)*
- [x] Each derivation emits an `Action` record; each conclusion is traceable to source
      (§10.2). *(G3.8 — one `deriver` Action per derive, recording premises, their source
      spans, kind, strength, and the conclusion/group/annotation outputs.)*
- [x] Conclusions carry both annotations; confidence comes from Layer B, not raw LLM.
      *(G3.8 — `value_conclusion` computes `support_count` from Layer A and `confidence`
      from Layer B over the augmented graph; the proposal's number never becomes the
      conclusion's confidence — "engine disposes". The LLM/rule **proposer** that generates
      candidate derivations is the documented upstream seam.)*

## Exit criteria

- [x] A conclusion derived from facts gains support; retracting a sole supporting fact
      retracts it; retracting one of several does not. *(G3.8 + G3.4 —
      `tests/integration/test_derive.py`: `deduce` gains support and a Layer B confidence;
      retracting the sole premise (stamp `valid_to`, reload via the adapter) drops the
      conclusion. The one-of-several exactness is the Layer A guarantee (G3.1
      `test_retracting_one_of_several_supports_keeps_conclusion`) the adapter now feeds.)*
- [ ] **Well-founded support holds:** an ungrounded `DERIVED_FROM` cycle retracts fully
      when its external base support is removed; a grounded cycle is kept.
- [~] Confidence is computed by Layer B and recomputed only on the affected sub-graph;
      an unfounded cycle never receives a confidence. *(G3.6 — computed by Layer B and the
      unfounded cycle gets no confidence; "only on the affected sub-graph" (incremental) is
      the deferred Layer B-incremental piece.)*
- [x] A cyclic derivation test converges (Layer B) **and** is correctly founded/unfounded
      (Layer A). *(G3.6 `test_cyclic_valuation_converges_to_a_gated_fixpoint` over Layer A's
      certified set; grounded vs unfounded cycle handling is the G3.1/G3.2 + G3.6 seam.)*
- [~] **Composed-loop termination:** the retraction feedback loop (REFUTES → retract →
      Layer A → Layer B → QBAF → …) runs with an **iteration bound + oscillation
      detection**; on non-convergence the unstable sub-region is surfaced as a finding,
      never silently re-iterated (§12, §7.2). *(G3.9 — the **driver** ships:
      `core/composed_loop.py::stabilize` bounds the iteration, detects oscillation (returns
      the cycle as the unstable region) and divergence, and **always terminates** with a
      structured outcome (`Stability.{CONVERGED,OSCILLATING,DIVERGED}`) — non-convergence is
      a finding, never re-iterated. The actual loop **body** (`REFUTES→A→B→QBAF`) needs the
      Phase 4 evidential/QBAF layer; Phase 4 supplies `step` and maps the unstable region to a
      graph finding. Monotonic-in-effort re-inference caching (§6.1) is a separate seam.)*
- [ ] (Feeds the validation gate jointly with Phase 4.)

## Phase risks / decisions

- **Well-founded support is a correctness requirement, not a nicety** (§12, §13). Plain
  Counting is correct only on acyclic regions; cyclic `DERIVED_FROM` SCCs **must** use
  DRed or clingo or an ungrounded cycle will hold itself up after retraction. Deterministic
  must-pass test, not a tune-to-fit gate.
- **Truth-maintenance placement** (in-Postgres Counting/DRed vs alongside DBSP) — MVP is
  in-Postgres; revisit only if retraction latency misses SLA (§13).
- The two-layer split is *our synthesis*, not a packaged result — validate the seam
  carefully (§13).
- Negation/aggregation in rules breaks plain provenance — restrict to stratified
  negation evaluated by clingo (or a recursion-safe IVM algorithm — not plain Counting)
  (§13).
- **Viterbi's depth bias is a real epistemic choice, not a tuning detail** (§12,
  review A6): under `max-·`, "confidence" partly measures derivation depth. Decide
  the semiring with the fixture before Layer B exists; switching later re-scores
  every conclusion and invalidates any fitted band thresholds.
