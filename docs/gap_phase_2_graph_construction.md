# Gap Plan — Phase 2 (Graph Construction)

**Why this file exists.** `todo_phase_2_graph_construction.md` is the requirement
list (referencing `architecture.md` by §); this file is the **build plan** — the
increment breakdown (G2.x), the design decisions taken, and the sequencing — mirroring
`gap_phase_1_ingest.md`. `architecture.md` remains the source of truth for every design
decision.

**Depends on:** Phase 0 (schema, `Box` model, audit log, AGE labels, domain-pack
scaffold) and Phase 1 (propositions with epistemic fields + faithfulness), both
substantially shipped.

**Refs:** §5 (reasoning-graph nodes), §5.2 (entity resolution), §6 (the `extract`
operator), §9 / §9.1 (tiers, boxes, credibility, sensitivity), §10 / §10.1 (schema,
action log), §14 (part-whole / abstraction level).

## Build order (cheap → foundational → expensive)

Thin slice first, then harden (the `todo.md` philosophy). Each increment is a coherent,
testable unit; entity resolution and level induction — the genuinely hard, downstream-
bounding pieces (§5.2, §14, §13) — come *after* the node-creation substrate exists.

| ID | Increment | Depends on | State |
|----|-----------|------------|-------|
| **G2.1** | **Box operationalization** — the box registry + case box; the shared Box↔AGE serialization the loader and indexes write through | Phase 0 | shipped |
| **G2.2** | **`extract` operator core** — proposition → `Fact` + `Actor`/`Object` nodes, `INVOLVES`(role) + `EVIDENCED_BY` edges, two annotations initialized, into a box, with an `Action`. **No dedup yet** (fresh nodes) | G2.1 | **shipped (this increment)** |
| G2.3 | Entity resolution subsystem (§5.2) — scored `SAME_AS` components, cheap→expensive cascade, conservative under-merge default + `candidate` links, anchor-canonicalizes to taxonomy | G2.2 | planned |
| G2.4 | Reference binding (§3.1) — detect `Mention`s separately from binding; scored `REFERS_TO` via the scoped cascade; low-confidence stays open → provisional → triage | G2.2 | planned |
| G2.5 | `PART_OF` abstraction levels (§14) — anchor-first to the pack taxonomy, induce fallback, coverage policy; level *derived* from the subject-role referent | G2.2 (+ G2.3 anchoring) | planned |
| G2.6 | Conditional credibility (§9.1) gated by epistemic class + sensitivity seeding onto facts | G2.2 | planned |
| G2.7 | Quarantine **enforcement** (Phase-1 G1.6) — provisional/low-faithfulness propositions cannot drive a `REFUTES` (gated until evidential edges exist) | Phase 4 edges | planned |

Cross-cutting (enforced from G2.2 on): every created node/edge has a non-empty
provenance path to `Span`(s) and a producing `Action` (§10.1/§10.2); two annotations
(integer support-count + `[0,1]` confidence) initialized on every reasoning node/edge
from day one (§12).

Phase-1 items unblocked by G2.1: **G1.11** (`box` on the dense/sparse indexes) and
**G1.8** (reference-corpus amortization) — both fast-follows now that a box contract and
the `case_box` constructor exist.

---

## G2.1 — Box operationalization *(shipped)*

**Goal.** Operationalize the **box** axis (§9): create/read/scope/deprecate the
lifecycle-provenance unit every node and edge carries, with a **case box** as the
`extract` operator's write target. Operationalize the **tier** axis as resolved-from-box,
override-allowed.

### What shipped

- **`iknos/boxes/serde.py` (pure, DB-free).** The single canonical Box↔AGE property
  mapping — `box_to_props` / `box_from_props` (round-trip inverse), `case_box`
  constructor (tier=case, deterministic `uuid5` id from `(name, version)`),
  `box_id_for`, and `resolve_tier(box, override)`. No `db`/`config` import, so the
  contract and constructors are unit-testable without a graph (same reason
  `core/proposition.py` keeps `db.age` lazy).
- **`iknos/boxes/registry.py` (DB).** `create_box` (create-if-absent, re-create is a
  true no-op returning the **stored** box — never moves `valid_from`), `get_box`,
  `list_boxes`, `active_boxes_by_tier` (ordered by `reliability_prior` desc — the
  reasoning-scope query), `deprecate_box`. Every lifecycle event emits an `Action`
  (§10.1). Caller owns the transaction (same contract as `load_pack`). `db.age` is
  imported lazily per function so the package stays DB-free to import.
- **`db/age.py`** gained the shared primitives `merge_vertex` / `merge_edge`
  (one MERGE-on-id implementation) and `unquote_agtype` / `parse_agtype_map`, promoted
  from the loader's privates.
- **`types/governance.py`** gained `SourceInterest.flatten()` (`interest_role` /
  `interest_stake`), mirroring `Sensitivity.flatten()`, so the conditional-credibility
  track (G2.6/§9.1) reads a stable property contract.
- **`domain/loader.py` consolidated** onto the shared layer: pack boxes serialize via
  `box_to_props` + write via `merge_vertex`, emit a `create-box` Action on first load,
  and `deprecate_pack` delegates to `deprecate_box`. Pack-specific *policy*
  (content-hash immutability, `PackImmutabilityError`) stays in the loader; only the
  serialization + write primitives are shared — so the two box-write paths cannot
  diverge (the divergence class that produced G0.R1).

### Decisions

- **One box-write path from day one.** The serialization contract (`box_to_props`) is
  shared between the registry and the pack loader, not duplicated. This is the explicit
  anti-tech-debt move: a second inline box-property dict would drift and re-introduce
  the G0.R1 `valid_from`-rewrite bug class.
- **Create-only `valid_from`, everywhere.** Generalized from packs to all boxes:
  re-creating an existing box is a no-op that preserves the bitemporal anchor. Box
  *metadata editing* (changing reliability/source) is deliberately **not** a re-create —
  it is a later governance/soft-override concern.
- **Deterministic case-box ids** (`uuid5(_BOX_NAMESPACE, name@version)`) so re-ingesting
  a case is idempotent. Packs keep their own namespace (their entity ids derive from the
  pack box id — never change it); the registry takes a fully-formed `Box`, so it imposes
  no id scheme.
- **Box lifecycle is auditable.** `create-box` / `deprecate-box` `Action`s are emitted
  for both registry and pack boxes — auditability from creation, not retrofitted
  (principle 9). (`action_type` is an open vocabulary; box lifecycle events sit
  alongside `promote`/`supersede`.)
- **No migration.** The `Box` label exists (migrations 0001/0004) and AGE is schemaless
  for properties; `actions` exists. G2.1 adds no Alembic revision.

### Deferred (kept out of the thin slice)

- **Working box** lifecycle (mutable, one-per-investigation, gated promotion) → Phase 6.
  G2.1 ships the **case box** (the §9 source box that holds a case document's
  observations/facts — the `extract` write target); conclusions/hypotheses/evidential
  edges live in the working box, which Phases 3/4/6 own. A `Box(tier=working)` can
  already be constructed and `create_box`'d; no dedicated constructor yet.
- **Box metadata editing** (`update_box`) → governance / Phase 7 (soft override).
- Threading `box` onto the Phase-1 indexes (G1.11) → fast-follow.

### Known limitations (bounded, recorded)

- `interest_stake` (and, by the same convention, `sensitivity_compartments`) persist as a
  JSON-encoded **string** property via `cypher_map`, so they are not natively
  Cypher-queryable by membership. Acceptable for G2.1 (no consumer queries stake yet);
  revisit if/when the credibility track needs stake containment queries.
- `deprecate_box` is not idempotent (re-deprecating moves `valid_to` and emits another
  `Action`) — deprecation is an explicit state transition, not a no-op like create. Fine
  for current use; guard if a re-deprecate path emerges.

### Tests

- **Unit (`tests/unit/test_boxes.py`, DB-free):** `box_to_props`/`box_from_props`
  round-trip (incl. `None` vs known-empty interest, JSON-string stake read shape, pack
  extras ignored), `SourceInterest.flatten()`, `case_box` determinism + tier, `resolve_tier`.
- **Integration (`tests/integration/test_box_registry.py`, live AGE):** case-box
  round-trip through `get_box`; re-create no-op with `valid_from` preserved + exactly one
  `create-box` Action; tier scoping ordered by reliability; deprecation closes the box and
  preserves `valid_from`; pack load emits the `create-box` Action. (Runs in CI; locally
  collected only when `DATABASE_URL` is set, per the integration conftest.)

### Exit criteria (G2.1)

- [x] Reference (pack) and case knowledge load into distinct boxes, queryable by box and
      jointly by tier (`active_boxes_by_tier`).
- [x] Every box creation/deprecation emits an `Action`; the pack and general box-write
      paths share one serialization contract.
- [x] Tier resolves from `Box` with an override hook (`resolve_tier`) for the `extract`
      operator (G2.2).
- [x] Re-creating a box is a no-op that preserves the bitemporal `valid_from`.

---

## G2.2 — `extract` operator core *(shipped)*

**Goal.** The `extract` operator (§6): turn a Phase-1 **Proposition** into a reasoning-graph
**Fact** carrying its **Actor**/**Object** entities as *nodes* (§5/§10), wired with
role-tagged `INVOLVES` and `EVIDENCED_BY` provenance, the **two annotations** initialized
(§12), into a case box, with an `Action` (§10.1). The node-creation substrate every later
Phase-2 slice builds on. **No entity dedup** (fresh nodes) — resolution is G2.3.

### What shipped

- **`iknos/core/extract.py`.** Pure/DB split on the `core/proposition.py` discipline:
  - *Pure (DB- and LLM-free, unit-testable):* the `NodeKind`/`Role` enums (the entity
    label and the `INVOLVES.role`, kept orthogonal); the guided-decode schema
    (`_EntityOut`/`FactEntities`, defaults keep a bare `{"label": …}` valid); the prompt
    (`SYSTEM_PROMPT`/`build_messages`, vocab generated from the same enums the schema is —
    no drift); `seed_confidence`/`base_annotations` (the §12 seed); and **`fact_to_props`**,
    the single canonical Fact→AGE write contract (cf. `box_to_props` for boxes).
  - *`Extractor`:* the operator. Three-phase like `propositionize_document` (the shared
    session is unsafe for concurrent use) — (1) serial idempotency filter against the
    `Action` log, (2) concurrent entity inference holding no DB session (semaphore-bounded),
    (3) serial per-fact persist, each its own short transaction. `extract_propositions`
    (batch) + `extract_proposition` (the §6 per-node shape). Writes go through the shared
    `merge_vertex`/`merge_edge` primitives, so the upsert discipline can't diverge.

### Decisions

- **Annotation seed (§12), not computation.** `support_count = 1` — a base fact is grounded
  by exactly one piece of evidence (its `EVIDENCED_BY` proposition); when that support is
  retracted the count drops to 0 (Layer A). `confidence` is seeded from the proposition's
  **faithfulness** (the only calibrated [0,1] available at extraction), or `1.0` — the
  Viterbi semiring identity, "no calibrated discount yet" — when no verifier ran. The real
  Layer-B confidence is the Phase-3 fixpoint; extraction only fills the slot so "both
  annotations from day one" holds. A `0.0` faithfulness is **not** swallowed by the `or`
  fallback (`None`-check, unit-tested).
- **Fresh nodes, no dedup.** Every mention becomes a new `Actor`/`Object` (no MERGE against
  an existing entity, no `SAME_AS`). Entity resolution into components is G2.3; building it
  here would couple the node substrate to the hardest, downstream-bounding piece (§5.2).
- **One Fact per proposition, routing preserved by provenance.** This slice materializes a
  Fact for every proposition; the §5 observation/judgement split ("a source's judgements are
  re-derived, not ingested as facts") is **not** applied here. `epistemic_class`/`routing`
  stay reachable via the `EVIDENCED_BY` Proposition (not duplicated onto the Fact), and
  treating judgement-claims as defeasible/credibility-weighted is the reasoning layer's job
  (Phase 3/4 + G2.6) — recorded as a seam, not silently dropped.
- **Idempotency keyed on the proposition id.** A proposition with an existing `extractor`
  `extract` Action is a true no-op (Action-table backed, mirroring `proposition._extracted_hash`).
  Re-extraction under a *changed* entity pipeline (cascade) is deferred; this slice only
  skips an already-extracted proposition.
- **Distinct actor in the Action log.** `actor="extractor"` (vs the propositionizer's
  `actor="propositionizer"`, both `action_type="extract"`) so the two extract passes never
  collide on the idempotency query.

### Deferred (kept out of the thin slice — documented seams)

- **Entity resolution / dedup** (scored `SAME_AS` components, anchor canonicalization) → G2.3.
- **Reference binding** (`Mention` → `REFERS_TO`) → G2.4.
- **Source credibility & sensitivity seeding** onto the Fact (§9.1): the Fact's confidence is
  seeded only from faithfulness (not box reliability) and its `sensitivity` is left at the
  lattice origin (public). Both → G2.6.
- **The §5 observation/judgement routing** of judgement propositions → Phase 3/4 + G2.6.
- **AGE property indexes** (G0.R2): `INVOLVES.role`/`box` and the entity-label `id`/`box`
  expression indexes the continuous resolution lookups need are the Phase-2 entry criterion,
  not this slice.

### Tests

- **Unit (`tests/unit/test_extract.py`, DB-free):** schema defaults / full record; prompt
  shape; the annotation seed (faithfulness passthrough, the `0.0`-not-swallowed guard, the
  `None`→`1.0` identity, `support_count==1`, pair uncollapsed); `fact_to_props` flattening
  (annotations, bitemporal null/open, sensitivity flat names, `override` omitted); and the
  mocked-LLM inference path (kind/role mapping, **two mentions → two fresh nodes**, empty list).
- **Integration (`tests/integration/test_extract.py`, live AGE):** end-to-end — Fact boxed +
  tiered-from-box + annotations + statement; `Actor`/`Object` with role-tagged `INVOLVES`;
  `EVIDENCED_BY` → Proposition *and* → Span resolving to source text (§10.2); the `extractor`
  Action joinable by output id (§10.1); **idempotent re-run** (no new Fact, no LLM call); the
  empty-entities Fact; and the batch driver skipping already-extracted propositions. (Runs in
  CI; locally collected only when `DATABASE_URL` is set, per the integration conftest.)

### Exit criteria (G2.2)

- [x] A Proposition becomes a `Fact` with its `Actor`/`Object` nodes, `INVOLVES`(role) and
      `EVIDENCED_BY` edges, both annotations, in the correct box and tier.
- [x] Every created node/edge has a non-empty provenance path to `Span`(s) and a producing
      `Action` (§10.1/§10.2).
- [x] Re-extracting a proposition is a no-op (no duplicate Fact).
