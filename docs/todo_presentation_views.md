# Presentation views — radar, table & coordinated projections

**Status:** optional track, layered on Phase 7 (expert interface). Not on the critical
path to the E1 go/no-go.

**Premise.** Every view is a *projection of the one graph* (§7 eng-doc, §14). This track
adds projections beyond the node-edge canvas: **read-only overview projections** (radar,
matrix, timeline) over the hypothesis/Task layer, and a **read + edit table/grid view**
for efficient mass review and bulk updates. All are coordinated with the node-edge
network and drill into it for audit.

**No backend/schema change.** Overview projections are read-only queries. The table's
bulk edits route through the **existing** soft-override + audit machinery (§10.3) — a bulk
action is N logged, reversible overrides in one transaction, not a new mutation path. Only
frontend + read/write projection queries.

---

## Scope & non-goals

- [ ] **In scope:** the **answer layer only** — Hypotheses, Tasks/sub-questions, and
      optionally key entities/subsystems (small-N). NOT propositions/facts (thousands →
      noise; they stay in the graph/detail views).
- [ ] **Read-only:** the radar renders graph state; **no reasoning happens on it**.
- [ ] It is an **overview / triage entry point**, not the audit surface — every element
      drills into the node-edge subgraph + provenance for the actual examination.

## No backend changes (confirm)

This track requires **no schema, no reasoning, and no new stored data**. It consumes
fields the engine already computes. The only non-frontend work is a thin **read-only
projection query/endpoint** that shapes existing graph data into the radar model.

- [ ] Confirm every channel below reads an **existing** field — add nothing to the schema.
- [ ] Projection query is read-only and box/clearance-scoped like any other view (§9.1).

## What it consumes (existing fields → radar channels)

- [ ] **Segments (angular)** ← the Task decomposition (`DECOMPOSES_INTO`): one wedge per
      sub-question. (Alt groupings: hypothesis `type`, or implicated `PART_OF` subsystem.)
- [ ] **Radial distance (centre = best)** ← Hypothesis `acceptability` (QBAF), **or** VoI
      (centre = settled, rim = contested). Must be a **real computed scalar** — never a
      faked/cosine distance (same discipline as §14 level).
- [ ] **Colour** ← Hypothesis `state` (supported/plausible/implausible/refuted), **or**
      the dominant uncertainty type (so a centre-but-fragile item is visibly flagged).
- [ ] **Size** ← `significance` / stakes (or evidence volume).
- [ ] **Trajectory / time** ← bitemporal valid-time (§7.4) — position over time.

## Radar view

- [ ] Render 180°/360° radar with configurable segments and rings; filterable by any
      field; zoom to a segment or show all.
- [ ] Plot the hypothesis/Task layer using the channel mapping above; legend + active
      encoding shown explicitly (which field is radius/colour/size right now).
- [ ] **Drill-down:** clicking a dot opens its SUPPORTS/REFUTES subgraph + resolved
      provenance in the node-edge view (the audit path). Selection is linked across views.
- [ ] **Don't render a verdict:** make contestedness visible (a "contested" ring band or
      colour = dominant uncertainty); show refuters/contradiction links as a light
      relational overlay so a bullseye dot is never read as "the answer."

## Non-monotonic trajectories (the differentiator)

- [ ] Animate **belief revision as motion**: a hypothesis migrating inward as support
      accrues, or outward to "refuted" when an overturning fact lands (drives off the
      bitemporal record — no new data).
- [ ] Optional time-scrubber: replay how the radar looked at time T ("what did we believe
      then?").

## Table / grid view — mass review & bulk update (read + edit)

The efficient surface for reviewing and updating *many* items at once — complement to the
one-at-a-time VoI queue and the spatial radar. Modeled on ITONICS's list view (sort/filter
/columns/conditional formatting + collective rating) and flowsint's entity tables.

- [ ] **Tabular projection** over any node/edge type (propositions, facts, hypotheses,
      entities, candidate merges, edges) — pick the type, get a grid.
- [ ] **Sort / filter / column selection by any field** — e.g. `epistemic_class =
      judgement AND faithfulness < 0.6`, or all `candidate` `SAME_AS` over a threshold.
      Conditional formatting on state / confidence / sensitivity.
- [ ] **Inline cell edit** = soft override (§10.3), same semantics as the canvas.
- [ ] **Multi-select → bulk actions:** confirm / reject / override a property /
      reclassify (e.g. observation↔judgement) / accept-or-reject candidate merges /
      assign for review — applied across the selection in **one logged, reversible
      transaction** (each row's change individually recorded, §10.1). Then delta
      re-propagation.
- [ ] **Bulk triage:** select a filtered slice of the VoI queue and dispose of it together
      (the common case: "confirm these 40 low-stakes provisional propositions").
- [ ] **Guardrails:** bulk edits obey clearance scope (§9.1); show an affected-count +
      preview before applying; every change is reversible and attributed.
- [ ] Linked selection with the canvas and radar (row ↔ node ↔ dot).

## Coordinated projections (siblings, same source of truth)

- [ ] **Linked selection** across radar ↔ node-edge graph ↔ review queue: select once,
      highlight everywhere.
- [ ] **Triage tie-in:** the contested rim ≈ the high-VoI region (§11.1) — let the radar
      double as a *spatial* view of the review queue (which wedge needs attention).
- [ ] (Optional) **Matrix/portfolio** projection (2 axes, e.g. significance × confidence)
      and **timeline/roadmap** projection (bitemporal evolution) — same read-only model,
      same drill-down. Build only if they earn their place.

## Dependencies

- Phase 6 — produces Hypotheses, `acceptability`, `state`, VoI, Task tree.
- Phase 7 — interface shell, node-edge view, provenance drill-down, review queue.
- §14 (projections / mixed-level frontier), §11.1 (VoI), §11.2 (Task), §7.4 (bitemporal).

## Exit criteria

- [ ] The radar plots the live hypothesis/Task layer from a read-only query, with no
      schema or reasoning additions.
- [ ] Channels are configurable; the active encoding is always legible.
- [ ] Every dot drills to its evidential subgraph + provenance; selection is linked across
      views.
- [ ] A hypothesis that is overturned visibly moves (rim → refuted) on new evidence.
- [ ] Nothing on the radar reads as a verdict — contestedness and refuters are visible.
- [ ] The table view filters/sorts any node/edge type and applies a **bulk update across a
      multi-selection in one logged, reversible transaction**, with clearance scope and a
      pre-apply preview.

## Validation

- [ ] Fold into the human-judgment trials (B-series): does the radar overview help an
      expert spot the contested answers and triage faster than the node-edge view alone?
      Measure, don't assume — drop it if it doesn't help.

## Risks / notes

- **Verdict-flattening** is the main risk: a centre dot implies "the answer," against
  "present the network, not a verdict." Mitigated by visible contestedness + mandatory
  drill-to-evidence; keep watching it in B-series review.
- **Multi-criteria overload:** stacking radius+colour+size+segment is powerful but can
  overwhelm; default to one or two channels, let the expert add more.
- Radar is for small-N (the answer layer). If it ever feels crowded, that means
  facts/propositions leaked in — they belong in the graph, not here.
