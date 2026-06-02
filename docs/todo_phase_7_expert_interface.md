# Phase 7 — Expert Interface: App, Audit & Override

**Goal:** the expert-facing graph analysis view where conclusions are reviewed, audited
to source, and corrected — with corrections soft, logged, reversible, and fed back as a
calibration signal. This is what makes Iknos an expert tool (principle 8) and closes
the human-in-the-loop.

**Depends on:** Phase 6 (runtime, presentation), and the audit/override plumbing from
Phases 0–2.
**Architecture refs:** §10.1 (process action log), §10.2 (per-node auditability), §10.3
(soft override + reconciliation), §6 (`app/`), principles 6, 8, 9.

## Graph analysis view (`app/`, §6)

- [ ] Interactive node-expansion canvas, performant on large graphs.
- [ ] Render ranked probable causes with their evidence subgraphs (from Phase 6
      `present`).
- [ ] Visualize sign/strength/significance on evidential edges; mark expert-set vs
      machine-derived values.

## Point auditability (§10.2)

- [ ] From any node/edge: show attributes, provenance (`EVIDENCED_BY` → spans → source
      text), the `Action` record(s) that produced/changed it, full bitemporal history,
      and any override.
- [ ] Process action log view (§10.1): the chronological narrative of what the system
      did; basis for replay.
- [ ] Enforce the invariant: nothing displayed that cannot answer "where did you come
      from."

## Soft override (§10.3) — expert-in-the-loop

- [ ] Override any computed attribute/content/edge from the graph view; **per-property**
      (e.g. edge `strength` vs `sign` tracked independently).
- [ ] **Soft, never destructive:** retain computed value; store `override`
      (overriding value, prior value, actor, timestamp, rationale); reversible.
- [ ] Overrides **participate in reasoning** (feed Layer A/B + QBAF, propagate) but are
      **marked**; the machine-only view is recoverable by ignoring the override layer.
- [ ] Each override is an `Action` (§10.1), bitemporal.

## Reconciliation policy (§10.3) — when re-derivation moves the value beneath an override

- [ ] **Default: hold with a divergence flag** — expert value stays; machine value
      updates beneath; drift shown.
- [ ] **Escalate to prompt** when *new evidence entered the basis* AND the change is
      material or crosses a state boundary — ask the expert to reconcile.
- [ ] **Auto-release on convergence** (machine within ε of override) — *suggest*
      dropping the now-redundant override; never silent.
- [ ] **Never auto-revert on divergence** — the machine must not overrule the human.
- [ ] Discriminator implemented: "did new information enter the basis?" (unchanged
      evidence → hold; grown evidence → prompt).

## Calibration feedback (§10.3, §13)

- [ ] Log divergence between expert-set and machine-set values as a per-operator /
      per-model **bias signal**; feed it back into the Phase 4 recalibration step.
- [ ] (Designing this feedback into recalibration is open work — close it here.)

## Exit criteria

- [ ] An expert can audit any node/edge to source and producing action.
- [ ] An expert can soft-override a value; original retained, override marked, logged,
      reversible; reasoning reflects it.
- [ ] Re-derivation under an active override behaves per the reconciliation policy.
- [ ] Override divergences are captured as a calibration signal.

## Phase risks / decisions

- Reconciliation must never silently discard expert judgment (inverts principle 6) nor
  silently let it go stale — both failure modes guarded by the policy.
- Prompt fatigue: only prompt on material change + new evidence, not every recompute.
