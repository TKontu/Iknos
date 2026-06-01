# Phase 3 — Reasoning Core: Two-Layer Propagation & Derivation

**Goal:** the novel core. Maintain *which* derived nodes are supported under retraction
(Layer A) and *how strongly* (Layer B), and derive conclusions from facts. No
off-the-shelf system packages this — it is the substance of the project.

**Depends on:** Phase 2 (nodes, `DERIVED_FROM` targets, both annotations present).
**Architecture refs:** §12 (two-layer model), §8 (decisions, staged build 1–2), §7.1
(edge confidence), §6 (`deduce`, `induce`).

## Layer A — truth maintenance over a commutative group (owns retraction)

- [ ] Per-derived-node integer **derivation-support count**; insertion increments,
      retraction decrements; node supported iff count > 0 (§12).
- [ ] **Counting** algorithm (recursion-capable) over the AGE graph for the MVP; DRed /
      Backward-Forward as references.
- [ ] Retraction propagation via `WITH RECURSIVE` closure over `DERIVED_FROM`; a
      conclusion survives if any support remains (counting discipline).
- [ ] Exactness check: deletion of one of several supports does **not** drop a node.
- [ ] (Scale path noted, not built: Differential Dataflow / DBSP fed by Postgres CDC.)

## Layer B — confidence valuation over an absorptive semiring (owns strength)

- [ ] Confidence as a **least fixpoint** over the **Viterbi** semiring
      `([0,1], max, ·, 0, 1)` — multiply along a rule body, max across alternative
      derivations (best-derivation confidence). (Gödel `max-min` as the ordinal
      alternative.)
- [ ] Compute only over nodes Layer A certifies as supported.
- [ ] Recompute incrementally on the **delta-affected sub-graph** Layer A reports.
- [ ] Verify convergence on **cyclic** derivation graphs (absorptive + ω-continuous →
      saturates, no inflation). **Never** use the sum-product semiring here.

## Two-layer integration

- [ ] Clean interface: Layer A decides membership → Layer B scores it. Two annotations,
      never merged (§12).
- [ ] Confirm idempotent confidence is *not* subtracted; retraction lives only in the
      group/count layer.

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
- [ ] Confidence is computed by Layer B and recomputed only on the affected sub-graph.
- [ ] A cyclic derivation test converges.
- [ ] (Feeds the validation gate jointly with Phase 4.)

## Phase risks / decisions

- **Truth-maintenance placement** (in-Postgres Counting vs alongside DBSP) — MVP is
  in-Postgres; revisit only if retraction latency misses SLA (§13).
- The two-layer split is *our synthesis*, not a packaged result — validate the seam
  carefully (§13).
- Negation/aggregation in rules breaks plain provenance — restrict to stratified
  negation with the recursion-capable Counting algorithm (§13).
