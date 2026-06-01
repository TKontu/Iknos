# Phase 2 — Graph Construction (Nodes)

**Goal:** populate the reasoning graph with typed nodes — facts, actors, objects —
organized into boxes/tiers, each traceable to source and logged.

**Depends on:** Phase 0 (schema, audit log, box registry), Phase 1 (propositions).
**Architecture refs:** §5 (reasoning graph nodes), §9 (tiers & boxes), §6 (operators,
`extract`), §10 (`INVOLVES`, `EVIDENCED_BY`), §10.1 (action log).

## Boxes & tiers (§9)

- [ ] Operationalize the **tier** axis (schema → reference → case → working) as the
      reasoning/entrenchment ordering; `tier` resolved from `Box`, override allowed.
- [ ] Operationalize the **box** axis (lifecycle/provenance unit): create, version,
      set reliability prior, status (active/deprecated).
- [ ] **Source vs working** boxes: source boxes append-on-ingest; one mutable working
      box per investigation (full lifecycle wiring in Phase 6).
- [ ] Box-scoped management operations (SQL by `box`); reasoning reads across active
      boxes by tier + reliability.
- [ ] Reference boxes are mostly TBox (rules/taxonomies); case boxes are ABox
      (observations) — reflect in how extraction populates each.

## Node extraction (the `extract` operator, §6)

- [ ] `extract`: proposition → `Fact` with `Actor`/`Object` nodes. Actors and objects
      are **nodes, not properties** (§5/§10).
- [ ] Entity deduplication across the active box set; canonical ids.
- [ ] `INVOLVES` edges (fact → actor/object) with `role`; `EVIDENCED_BY` edges (fact →
      proposition/span).
- [ ] Seed each fact's source-reliability/`significance` prior from its box tier (§9,
      feeds Phase 4 edge significance).
- [ ] Both annotations initialized: support-count and confidence (§12).

## Provenance & audit (cross-cutting, enforced here)

- [ ] Every created node/edge has a non-empty provenance path to `Span`(s) (§10).
- [ ] Every `extract` run emits an `Action` record: inputs (spans/propositions),
      outputs (node ids), model, sampling (§10.1).
- [ ] Verify per-node auditability: from a `Fact`, reach its spans, source text, and
      producing `Action` (§10.2).

## Exit criteria

- [ ] Ingested propositions become a deduplicated graph of facts/actors/objects in the
      correct box and tier.
- [ ] No node or edge exists without provenance and an `Action` record.
- [ ] Reference vs case knowledge can be loaded into distinct boxes and queried both
      separately (by box) and jointly (by tier).

## Phase risks / decisions

- Entity dedup across boxes is where cross-box edges/identity live — get canonical ids
  right; it underpins candidate generation (§5.1) later.
- Keep extraction's typed `Actor`/`Object` as the node source — never fall back to
  word-frequency keywords for nodes (§4).
