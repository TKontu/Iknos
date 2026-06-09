# Phase 3 — Reasoning Core: Two-Layer Propagation & Derivation

**Goal:** the novel core. Maintain *which* derived nodes are supported under retraction
(Layer A) and *how strongly* (Layer B), and derive conclusions from facts. No
off-the-shelf system packages this — it is the substance of the project.

**Depends on:** Phase 2 (nodes, `DERIVED_FROM` targets, both annotations present).
**Architecture refs:** §12 (two-layer model), §8 (decisions, staged build 1–2), §7.1
(edge confidence), §6 (`deduce`, `induce`).

## Layer A — truth maintenance over a commutative group (owns retraction)

- [ ] **Well-founded support is the definition:** a node is supported iff it is in the
      least fixpoint grounded in **base facts** (`EVIDENCED_BY` leaves / axiomatic rules)
      and closed under derivations (§12). Mark base facts explicitly as the grounding
      anchor. The integer support-count is the incremental *implementation*, not the
      definition.
- [ ] **Acyclic regions:** Counting over `DERIVED_FROM` (cheap, correct here);
      retraction via `WITH RECURSIVE` closure; a conclusion survives if support remains.
- [ ] **Cyclic regions (nontrivial `DERIVED_FROM` SCCs):** detect SCCs and route them to
      a cycle-safe algorithm — **DRed (over-delete reachable, then re-derive only what
      re-grounds in base facts)** — or hand foundedness to **clingo** (ASP unfounded-set
      elimination). Plain Counting is **not** correct here.
- [ ] **Correctness tests (must-pass, deterministic):** an *ungrounded* derivation cycle
      retracts fully when its external base support is removed; a *grounded* mutual-
      support pair (also reaching base) is correctly kept.
- [ ] Exactness check: deletion of one of several supports does **not** drop a node.
- [ ] (Scale path noted, not built: Differential Dataflow / DBSP fed by Postgres CDC —
      recursive retraction correct by construction.)

## Layer B — confidence valuation over an absorptive semiring (owns strength)

- [ ] Confidence as a **least fixpoint** over the **Viterbi** semiring
      `([0,1], max, ·, 0, 1)` — multiply along a rule body, max across alternative
      derivations (best-derivation confidence). (Gödel `max-min` as the ordinal
      alternative.)
- [ ] Compute only over nodes Layer A certifies as **well-founded**-supported (so an
      unfounded cycle never receives a confidence — foundedness gates scoring).
- [ ] Recompute incrementally on the **delta-affected sub-graph** Layer A reports.
- [ ] Verify convergence on **cyclic** derivation graphs (absorptive + ω-continuous →
      saturates, no inflation). **Never** use the sum-product semiring here.

## Two-layer integration

- [ ] Clean interface: Layer A decides membership → Layer B scores it. Two annotations,
      never merged (§12).
- [ ] Confirm idempotent confidence is *not* subtracted; retraction lives only in the
      group/count layer.
- [ ] **Aggregate evidence over `SAME_AS` components (§5.2):** support counts and
      confidence accrue to the canonical component, not the raw node. A **merge/split**
      (assert/retract `SAME_AS`) is a belief-revision trigger — re-run Layer A/B over the
      affected component only.

## Derivation operators (§6)

- [ ] `deduce`: facts/conclusions → `DeductiveConclusion`, with `DERIVED_FROM` edges
      and provenance to underlying facts' spans.
- [ ] `induce`: facts → `InductiveConclusion`, marked provisional.
- [ ] Each derivation emits an `Action` record; each conclusion is traceable to source
      (§10.2).
- [ ] Conclusions carry both annotations; confidence comes from Layer B, not raw LLM.

## Exit criteria

- [ ] A conclusion derived from facts gains support; retracting a sole supporting fact
      retracts it; retracting one of several does not.
- [ ] **Well-founded support holds:** an ungrounded `DERIVED_FROM` cycle retracts fully
      when its external base support is removed; a grounded cycle is kept.
- [ ] Confidence is computed by Layer B and recomputed only on the affected sub-graph;
      an unfounded cycle never receives a confidence.
- [ ] A cyclic derivation test converges (Layer B) **and** is correctly founded/unfounded
      (Layer A).
- [ ] **Composed-loop termination:** the retraction feedback loop (REFUTES → retract →
      Layer A → Layer B → QBAF → …) runs with an **iteration bound + oscillation
      detection**; on non-convergence the unstable sub-region is surfaced as a finding,
      never silently re-iterated (§12, §7.2).
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
