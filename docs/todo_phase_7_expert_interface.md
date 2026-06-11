# Phase 7 — Expert Interface: App, Audit & Override

**Goal:** the expert-facing graph analysis view where conclusions are reviewed, audited
to source, and corrected — with corrections soft, logged, reversible, and fed back as a
calibration signal. This is what makes Iknos an expert tool (principle 8) and closes
the human-in-the-loop.

**Depends on:** Phase 6 (runtime, presentation), and the audit/override plumbing from
Phases 0–2.
**Architecture refs:** §10.1 (process action log), §10.2 (per-node auditability), §10.3
(soft override + reconciliation), §6 (`app/`), principles 6, 8, 9.

## Entry criteria *(added by the 2026-06-11 review, M5)*

- [ ] **`docs/design_app_stack.md` — the front-end stack decision.** Decide before
      any `app/` code: framework (the flowsint pattern this phase borrows is
      React-based — adopting it is the default to justify or refute), graph canvas
      (Cytoscape.js / Sigma.js / React Flow — evaluated against the §6 "performant
      on large graphs" requirement), state/streaming client for the §6 event stream,
      and build tooling. License-check every candidate against principle 7
      (permissive only). One recommendation, alternatives dismissed with reasons.
- [ ] **Canvas performance spike.** Before committing to the chosen canvas: render a
      synthetic graph at 10× expected investigation size (use the C3 benchmark
      generator's shape) with expand/collapse + linked selection; record
      frames/interaction latency in the design doc. This is the load-bearing
      assumption of the whole view — measured, not hoped.

- [ ] Interactive node-expansion canvas, performant on large graphs. **This is the
      node-edge *projection* — one of several coordinated views of the one graph (§14);
      the audit/relational surface that other projections drill into.**
- [ ] Render ranked probable causes with their evidence subgraphs (from Phase 6
      `present`).
- [ ] Visualize sign/strength/significance on evidential edges; mark expert-set vs
      machine-derived values.
- [ ] **Coordinated views / linked selection:** selecting an item syncs across the
      node-edge view, the review queue, and any optional projections
      (`todo_presentation_views.md` — radar/matrix/timeline). View-switching preserves
      selection and filter scope. The node-edge view is the drill-down target for all of
      them.
- [ ] **Abstraction-level controls (§14):** switch audience level (management ↔
      expert) as a cut through the `PART_OF` hierarchy; expand/collapse a subtree to
      adjust the mixed-level frontier interactively; show each region at its most
      relevant level.
- [ ] Allow expert **override of `PART_OF` attachment** (re-parent an entity / correct
      a fact's level) as a soft override (§10.3) like any other edge.

## Interaction & editing — direct manipulation (flowsint-style)

- [ ] **Edit any value inline = soft override (§10.3).** Click an attribute / edge
      weight / classification and change it; the UI is direct editing, the semantics are a
      logged, reversible, bitemporal override (the computed value is retained underneath).
- [ ] **Drag to re-link:** reconnect or redirect an edge (re-attach `REFERS_TO`, re-parent
      `PART_OF`, add/redirect `SUPPORTS`/`REFUTES`) as an override.
- [ ] **Manually assert content:** add nodes, edges, and facts directly from the canvas
      (an entity, a relationship, a known fact / testimony). Expert-asserted content is
      **attributed to the expert** (source = expert, `epistemic_class`, credibility) and
      enters the same machinery — it is evidence, **not privileged ground truth**, logged
      and provenanced like everything else.
- [ ] **Invoke operators on demand (flowsint-style):** run an operator on a selected node
      / region from the canvas — `extract` · `corroborate` · `find-contradiction` ·
      `deduce` · `expand candidates` — so the analyst can *drive* the investigation, not
      only consume the autonomous loop. Same operators as §11; results land in the working
      box and re-propagate.
- [ ] **All edits non-destructive & re-propagating:** every inline edit, manual assertion,
      drag, and on-demand operator run is logged (§10.1), reversible, bitemporal, and
      triggers delta-scoped re-propagation (§6.1, §12). No silent or hard mutation.
- [ ] Real-time updates via the §6 `api` event stream; canvas responsive on large graphs.

## Point auditability (§10.2)

- [ ] From any node/edge: show attributes, provenance (`EVIDENCED_BY` → spans → source
      text), the `Action` record(s) that produced/changed it, full bitemporal history,
      and any override.
- [ ] Process action log view (§10.1): the chronological narrative of what the system
      did; basis for replay.
- [ ] Enforce the invariant: nothing displayed that cannot answer "where did you come
      from."
- [ ] **Clearance-filtered views (§9.1):** present only nodes at-or-below the viewer's
      clearance and within their compartments; a visible conclusion whose provenance the
      viewer is not cleared for shows the trail redacted (auditability is relative to
      clearance).

## Task framing (§11.2)

- [ ] Enter and edit the **`Task`** (framing question + type); view its `answer_state`
      and the answer (addressing hypotheses, banded true/plausible/implausible/false).
- [ ] View and **edit the decomposition tree** (`DECOMPOSES_INTO`) — accept/prune/add
      sub-Tasks; decomposition is LLM-proposed but expert-editable (principle 6).
- [ ] **Seed and edit hypotheses** — from decomposition, the domain pack's reference
      hypothesis set, or entered by hand; each `ADDRESSES` a Task.

## Review queue — value-of-information triage (§11.1)

- [ ] Present a **ranked, budgeted review queue** ("review these N first"), ordered by
      VoI from Phase 6 triage — not the whole graph, not whatever is on screen.
- [ ] Each item states **what turns on it** (which hypotheses move, by how much) and
      **what judgment is needed** (confirm a referent / weigh evidence / accept-reject a
      merge / reconcile an override), derived from the dominant uncertainty type.
- [ ] Surface **fragile-confidence** and **conflicting-confidence** items, not only
      low-confidence ones (guard against confident-wrong).
- [ ] Re-rank **between batches**, not per action; show the VoI decomposition (leverage,
      uncertainty type, stakes) so the expert sees why they are needed (principle 9).
- [ ] Feed all needs-human signals into this one queue: provisional propositions (§3.1),
      ambiguous bindings (§3.1), candidate merges (§5.2), unresolved/cyclic regions
      (§13), override-reconciliation prompts (§10.3).

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

## Leads & inquiries (§11.3 — merged from `archive/todo_ingest.md` 2026-06-11)

- [ ] Surface the ranked lead list (next-best-move) beside the review queue — same
      VoI-per-unit-cost ordering, marked clearly as *advisory* (inert until accepted).
- [ ] Accept / reject / edit a lead; accepting an external lead creates the
      `Task.kind = inquiry` sub-Task; accepting an internal move runs the operator
      (the §6 on-demand invocation path).
- [ ] Enter an inquiry by hand (the expert proposes their own acquisition); track its
      `answer_state`; on completion, attach the acquired source and trigger re-ingest.
- [ ] Remediation answers (normative Tasks) presented as decision support — never
      auto-executed, never re-injected as evidence (§11.3).
