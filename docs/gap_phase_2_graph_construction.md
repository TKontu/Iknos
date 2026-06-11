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
| **G2.3** | Entity resolution subsystem (§5.2) — scored `SAME_AS` components, cheap→expensive cascade, conservative under-merge default + `candidate` links | G2.2 | **shipped (this increment)** — thin slice; anchor-canonicalization + belief-revision/contradiction loop deferred |
| G2.4 | Reference binding (§3.1) — detect `Mention`s separately from binding; scored `REFERS_TO` via the scoped cascade; low-confidence stays open → provisional → triage | G2.2 | **shipped (this increment)** — thin slice; pronoun/discourse-antecedent + taxonomy-anchor stages + multi-sample/verify confidence deferred |
| G2.5 | `PART_OF` abstraction levels (§14) — anchor-first to the pack taxonomy, induce fallback, coverage policy; level *derived* from the subject-role referent | G2.2 (+ G2.3 anchoring) | **shipped (this increment)** — the induce path + cycle-safe `partOf` closure + derived-level read; anchoring (needs entity-linking) + embedding/IC level estimation deferred |
| G2.6 | Conditional credibility (§9.1) gated by epistemic class + sensitivity seeding onto facts | G2.2 | **shipped (this increment)** — derived-not-stored credibility computation + sensitivity seed; the per-claim alignment-judging pass deferred |
| G2.7 | Provenance & audit (§10.2) — checkable Fact reach-back (Fact → spans → source text → producing `Action`) + the box-level invariant, backed by a partial functional index | G2.2 | **shipped (#42)** — `provenance/audit`; the universal per-node/edge crawler is the deferred seam |
| **G2.8** | **Entity-linking / taxonomy anchoring** (§5.2/§9/§14) — link case entities to the active pack taxonomy via a scored `ANCHORS_TO`; the *primary, reliable* identity/level path G2.3/G2.4/G2.5 defer to | G2.2 (+ G0.7 packs) | **shipped (this increment — slice 1)** — the linking operator + `ANCHORS_TO` edges + coverage metric; the anchor-canonicalization fold (into `resolve`) + anchor-first level (into `partwhole`) + the reference-binder taxonomy stage are slice 2 |
| G2.9 | Quarantine **enforcement** (Phase-1 G1.6) — provisional/low-faithfulness propositions cannot drive a `REFUTES` (gated until evidential edges exist) | Phase 4 edges (G4.3 s3) | **shipped (this increment)** — categorical sign gate (a provisional source may not drive a `REFUTES`); the stakes-dependent *continuous* faithfulness-vs-significance cutoff is the deferred calibration seam (Trial A5 / G4.6) |

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

---

## G2.3 — Entity resolution subsystem *(shipped — thin slice)*

**Goal.** Resolve the **fresh, un-deduplicated** `Actor`/`Object` nodes G2.2 emits (two
mentions of one entity → two nodes) into canonical entities (§5.2). Identity is a
**defeasible, scored assertion**, never a destructive id reassignment: two entities are "the
same" only via a scored `SAME_AS` edge, and the canonical entity is the `SAME_AS`-connected
component — reasoning aggregates evidence at the component level. Resolution is a
cheap→expensive **cascade** (block → score → resolve) with a **conservative under-merge
default**: auto-merge only above a high confidence bar (`confirmed`); below it a `candidate`
edge keeps the entities separate but the fragmentation visible and the evidence bridgeable.

### What shipped

- **`iknos/core/resolve.py`.** Pure/DB split on the `core/extract.py` discipline:
  - *Pure (DB- and LLM-free, unit-testable):* `normalize_label` (the blocking + exact-agreement
    key); `block_candidates` (the cheap stage — shared-token pairs within a `NodeKind`, via a
    token→entities inverted index); `score_pair` (the deterministic **relational/contextual**
    score, §5.2); `decide` (the conservative bars); **`same_as_to_props`** (the single canonical
    `SAME_AS` write contract, cf. `extract.fact_to_props`); and `components`/`canonical_id`
    (union-find over `confirmed` edges → canonical components).
  - *`Resolver`:* the operator. `_load_entities` reads a box's `Actor`/`Object` nodes with their
    `INVOLVES` roles and a **relational context fingerprint** (the normalized labels of
    co-involved entities); `resolve_box` runs load → block → score → decide → persist with one
    `resolve` Action (`actor="entity-resolver"`); `canonical_components` is the component read
    reasoning consumes. Writes go through the shared `merge_edge` primitive, in a canonical
    endpoint direction (min-id → max-id), so the edge set is a structurally-idempotent function
    of the box contents.
- **`iknos/types/edges.py`** gained `SameAsState` (`candidate`/`confirmed`, the §10 edge state).

### Decisions

- **Deterministic relational scoring, no LLM in the resolve path** (user-confirmed). §5.2 scores
  on shared facts/roles/attributes and **bars similarity from scoring** (similarity is a
  *blocking* signal only). The slice scores on exact attribute *agreement* (same normalized
  label / type — legitimate evidence) plus relational context (shared co-involved labels, shared
  role); fuzzy/embedding similarity never enters the score. A conflicting non-empty type is
  disconfirming.
- **Conservative under-merge, calibrated by the bars.** Weights are set so exact label + agreeing
  type *alone* (0.75) lands in the candidate band — never an auto-merge; only added relational
  context crosses the confirm bar (0.85). Over-merge fabricates contradictions and corrupts
  reasoning, so under-merge is the safer failure (§5.2).
- **Label-based relational fingerprint.** The co-involved entities are themselves un-resolved
  fresh nodes this pass, so "shared facts" is computed over the *normalized labels* of
  neighbours, not their ids — genuine relational evidence rather than surface similarity of the
  entity itself.
- **Structural idempotency.** `SAME_AS` is written via the upsert `merge_edge` keyed on
  endpoints+label, in canonical direction — re-resolving an unchanged box recomputes the same
  edges and writes no duplicates. No proposition-style Action-log skip is needed; the resolve
  Action is an audit record per run.
- **No migration.** `SAME_AS` exists (migration 0004) and the 0007 label indexes cover
  `Actor`/`Object`/`INVOLVES`/`SAME_AS`.

### Deferred (kept out of the thin slice — documented seams)

- **Blocking signals beyond lexical/type** — embedding-neighbourhood and taxonomy-anchor
  blocking (§5.2) need an entity-embedding store / G2.4–G2.5 entity-linking.
- **Anchor canonicalization** — a mention that entity-links to the pack taxonomy takes that node
  as its canonical identity (§5.2/§14) → G2.4/G2.5.
- **Merge/split as belief revision** — asserting/retracting a `SAME_AS` should re-run Layer A/B
  over the affected component → Phase 3. This slice writes edges; it does not re-run reasoning.
- **Contradiction→split-review loop + hysteresis** (§5.2) → needs `find-contradiction` (Phase 4).
- **Cross-box `SAME_AS`** (belongs to the working box, §9) → this slice resolves within a source
  box (the caller passes one box's entities).
- **Expert-triage queue** for `candidate` merges → Phase 7; the slice records the `candidate`
  edge the queue will later consume.

### Tests

- **Unit (`tests/unit/test_resolve.py`, DB-free):** `normalize_label`; `block_candidates`
  (shared-token same-kind pairing, cross-kind exclusion, multi-token dedup); `score_pair`
  (confirm on label+type+context, candidate on label+type alone, conflicting-type suppression,
  distinct-label rejection, relational monotonicity); `decide` boundaries; `same_as_to_props`
  flattening (state, strength, two annotations, open bitemporal); `components` union-find
  (transitive collapse, singleton omission) + `canonical_id`.
- **Integration (`tests/integration/test_resolve.py`, live AGE):** seed a box via the extractor
  (mocked LLM) → resolve → two `confirmed` `SAME_AS` with strength + annotations; canonical
  components exposed with their representative; the `entity-resolver` Action joinable; idempotent
  re-run (no duplicate edges). A second box exercises the `candidate` band (no component formed).
  (Runs in CI; locally collected only when `DATABASE_URL` is set, per the integration conftest.)

### Exit criteria (G2.3)

- [x] Fresh `Actor`/`Object` nodes resolve into scored `SAME_AS` components via a cheap→expensive
      cascade, box-scoped.
- [x] The default is conservative under-merge: `confirmed` only above a high bar, else a
      bridgeable `candidate` edge that keeps entities separate.
- [x] The canonical entity is the `confirmed`-`SAME_AS`-connected component, read for evidence
      aggregation; every run emits an `Action`.
- [x] Re-resolving a box writes no duplicate edges (structural idempotency).

---

## G2.4 — Reference binding *(shipped — thin slice)*

**Goal.** A proposition's surface references — a pronoun ("it"), a definite description
("the bearing"), a named reference ("bearing 3") — denote entities already in the graph.
**Reference binding is a separate, scored decision, not resolved invisibly** (§3.1):
*detecting* that a mention needs a referent is robust, but choosing *which* entity is
error-prone, so the two steps are split (as sign is split from magnitude in §8). G2.4
detects `Mention`s and binds each to a canonical entity via a defeasible, confidence-bearing
`REFERS_TO` edge through the scoped cascade (local antecedent → in-graph entity → taxonomy →
unresolved). The default is **conservative**: a binding is `confirmed` only when a single
referent clears a high bar; otherwise it stays **open** (one or more `candidate` edges) and
the dependent proposition is marked `provisional` and routed to expert triage — an
over-eager binding silently fabricates coreference and corrupts every downstream derivation.

### What shipped

- **`iknos/core/reference.py`.** Pure/DB split on the `core/resolve.py` discipline:
  - *Pure (DB-free, unit-testable):* `MentionType` (pronoun/definite/proper); the detection
    schema (`_MentionOut`/`DetectedMentions`) + prompt (`SYSTEM_PROMPT`/`build_messages`,
    vocab generated from the same enums); `group_referents` (collapse same-label fresh nodes
    into one canonical referent, with `exclude_ids` for the no-self-bind rule);
    `block_referents` (the cheap stage — shared-token, kind-scoped); `score_binding` (the
    deterministic lexical+attribute score — containment + exact label + kind agreement,
    **never** similarity/attention); `decide_binding` (the conservative bars + tie handling);
    and the canonical write contracts `mention_to_props` / `refers_to_to_props`.
  - *`ReferenceBinder`:* the operator (`actor="reference-binder"`, `action_type="bind"`).
    Box-scoped like the resolver; three-phase like the extractor — (1) serial Action-log
    idempotency filter, (2) concurrent **detection** holding no DB session (LLM detection
    only), (3) serial per-proposition persist (Mention vertices + `EVIDENCED_BY` provenance +
    scored `REFERS_TO` + provisional OR-fold + one `bind` Action). Writes go through the
    shared `merge_vertex`/`merge_edge` primitives.
- **`iknos/types/edges.py`** gained `BindingState` (`candidate`/`confirmed`, mirroring
  `SameAsState`); an unresolved mention writes **no** edge (the absence is the state).

### Decisions

- **Detection ≠ binding (§3.1).** The LLM does detection only and proposes *no* binding; the
  binding score is deterministic (the `resolve.score_pair` precedent — no LLM in the scoring
  path). Attention/embedding similarity never scores a binding; the lexical signal is
  *containment* of the mention's surface in a referent's label (a referring expression is
  typically a shorter form of a fuller name — "the bearing" ⊂ "bearing 3").
- **No self-binding.** A mention's referent pool **excludes its own proposition's** extracted
  entities — the same-clause entity is already captured by `INVOLVES`, not coreference; the
  antecedent is elsewhere. Without this, a definite description trivially confirm-binds to the
  fresh node extracted from its own clause and never discovers the cross-proposition referent.
  This is what makes binding do real work (`group_referents(exclude_ids=…)`).
- **Conservative, calibrated by the bars.** `confirm` (0.85) requires a *single* referent at
  an exact label + agreeing kind; a near-tie (within `TIE_MARGIN`) or a merely-contained
  partial match lands in the `candidate` band — kept **open** with one edge per tied referent
  (§3.1 "multiple candidate targets when ambiguous"), and the proposition marked provisional.
- **Provisional OR-fold (§3.1/G1.6).** A proposition resting on an unresolved or only
  candidate-bound mention is set `provisional = true`; never cleared here (the proposition
  layer's OR-fold discipline).
- **Referents are label-grouped, not component-keyed.** Same-label fresh nodes collapse to one
  referent at the canonical-min id (the `resolve.canonical_id` representative), so binding is
  robust whether or not entity resolution (G2.3) has run — it need not be ordered after it.
- **Idempotency keyed on the proposition id.** A proposition with an existing `bind` Action is
  a no-op (Action-table backed, mirroring `extract._already_extracted`) — including a
  mention-less proposition, so a re-run over a box settles. Re-binding under a changed pipeline
  or after new entities arrive is belief revision (Phase 3).
- **No migration.** `Mention` / `REFERS_TO` exist (migration 0004) and the 0007 label indexes
  cover both.

### Deferred (kept out of the thin slice — documented seams)

- **Pronoun anaphora / the local-discourse-antecedent stage.** A bare pronoun has no lexical
  content, so the in-graph-entity stage blocks it to the empty set — this slice **detects** it
  and leaves it unresolved (→ provisional), the correct conservative behaviour. Binding it
  needs the discourse-order antecedent stage (a dedicated coreference model, §3.1).
- **Taxonomy-anchor stage** — binding a mention to a domain-pack taxonomy node needs
  entity-linking → G2.5 (with the part-whole anchoring).
- **Relational tie-break** — when several same-kind referents match a definite description
  equally, this slice keeps them all `candidate`; using shared-fact/role context to break the
  tie (the `resolve.score_pair` relational signal) is the natural enhancement.
- **Multi-sample / verify confidence** (§3.1 "confidence from consistency + verification") —
  this slice's confidence is the single deterministic binding score.
- **Re-binding as belief revision** (re-run Layer A/B over the affected proposition) → Phase 3.
- **Expert-triage queue** for open bindings → Phase 7; the slice marks the proposition
  provisional and records the `candidate` edges the queue will later consume.

### Tests

- **Unit (`tests/unit/test_reference.py`, DB-free):** referent grouping (collapse, kind
  separation, empty-label drop, `exclude_ids` self-bind exclusion + emptied-group drop);
  blocking (shared-token requirement, pronoun→empty, kind-guess narrowing); scoring (exact
  confirm, containment-only candidate, no-overlap/pronoun zero, partial-overlap monotonicity);
  the decision bars (single-exact confirm, unresolved, tied candidates, single-partial open,
  deterministic target ordering); and the `Mention`/`REFERS_TO` write contracts.
- **Integration (`tests/integration/test_reference.py`, live AGE):** seed a box via the
  extractor (mocked LLM) → bind (mocked detector) → a `confirmed` `REFERS_TO` to a prior named
  entity with the proposition left non-provisional + the `bind` Action joinable + idempotent
  re-run (no duplicate mentions/edges); an ambiguous definite description → two `candidate`
  edges + provisional; a pronoun → no edge, Mention recorded, provisional. (Runs in CI;
  locally collected only when `DATABASE_URL` is set.)

### Exit criteria (G2.4)

- [x] A proposition's surface references become `Mention` nodes (detection), provenance-linked
      to their Span(s), separate from binding.
- [x] Each mention binds to a canonical entity via a scored `REFERS_TO` through the in-graph
      cascade stage; the score is deterministic (no attention), conservative by the bars.
- [x] Ambiguous/low-confidence bindings stay **open** (multiple `candidate` targets / no edge)
      and mark the dependent proposition `provisional`; every run emits an `Action`.
- [x] Re-binding a settled proposition is a no-op (Action-log idempotency).

---

## G2.6 — Conditional credibility + sensitivity seeding *(shipped — thin slice)*

**Goal.** Wire the two §9.1 governance quantities a Fact carries from the source, **keeping
credibility derived and sensitivity propagated** — never a flat stored scalar (§9.1/§10).
(1) **Sensitivity** seeds onto each base Fact as the lub of its source Span(s) — the §9.1
information-flow high-water-mark, base case of the provenance propagation. (2) **Credibility
is conditional and gated by epistemic class**: this increment ships the canonical
*computation* over the stored inputs (box `reliability_prior` × an interest modifier that an
**observation** ignores and a **judgement** applies fully) — **derived at use-time, never
stored**, so a stored scalar can't collapse the conditional nature or fix it against later
belief revision.

### What shipped

- **`iknos/core/credibility.py` (pure + one DB read).** `interest_modifier` (the §9.1
  modifier interpolated from identity toward the alignment endpoint by the epistemic-class
  *gate* — observation gate 0 ⇒ always 1.0; judgement gate 1 ⇒ full discount/boost) and
  `effective_credibility` (`reliability_prior × modifier`, clamped) — both DB-free,
  fail-loud on an unmapped enum (the `_ROUTING`/`_SENSITIVITY_RANK` convention).
  `effective_credibility_of(session, fact_id)` is the use-time read: it walks Fact→Box
  (`reliability_prior`), Fact→Proposition (`epistemic_class`), and the Fact's
  `interest_alignment` slot, returning `None` when the chain is incomplete.
- **`iknos/types/governance.py`** gained `InterestAlignment`
  (`self-serving`/`neutral`/`against-interest`/`unknown`) — the derived per-claim credibility
  input — and `Sensitivity.from_props` (the read inverse of `flatten`, decoding the
  JSON-string compartment property; absent level ⇒ public origin).
- **`iknos/core/extract.py`** now seeds the Fact's sensitivity from its source spans
  (`seed_sensitivity` = lub-fold; `_span_sensitivities` reads them via
  `Sensitivity.from_props`) instead of leaving the lattice origin, and serializes the
  `interest_alignment` slot (omitted while `None`). **`iknos/types/nodes.py`** Fact gained
  the `interest_alignment` field (the schema-contract placeholder).

### Decisions

- **Credibility is derived, never stored (§9.1/§10).** Phase 2 seeds the *inputs* only; the
  scalar is computed by `effective_credibility_of` at use-time (Phase 4's adjudication — the
  §8↔credibility seam) and may be materialized later as a recomputed cache, like level (§14).
  A stored credibility number is the explicit anti-pattern §9.1 forbids.
- **Gated by epistemic class, in the formula.** The observation/judgement split is a property
  of `interest_modifier` (the class gate), not a caller branch — "credibility applies where it
  matters" can't be forgotten at a call site. Observation credibility is interest-independent;
  judgement/testimony are interest-weighted (self-serving discounted, against-interest boosted
  to the clamp ceiling — an admission against interest is maximally credible).
- **Unknown alignment is the identity, not a penalty.** A Fact's `interest_alignment` is `None`
  until the (deferred) judging pass runs; the read coerces it to `UNKNOWN` (modifier 1.0), so
  an un-judged claim's credibility is just the box reliability — defer, never penalize on
  absence (the `faithfulness`/`provisional` placeholder convention).
- **Sensitivity seed is the base case of the §9.1 propagation.** A base Fact's antecedents are
  its source Span(s); its sensitivity is their lub. The `DERIVED_FROM` walk that carries
  sensitivity to *conclusions* (the lub over a derivation's antecedents) stays deferred to
  Phase 3/5 — this increment does the base layer the walk will build on.
- **No migration.** AGE is schemaless for properties; `interest_alignment` /
  `sensitivity_*` are plain property strings on existing labels.

### Deferred (kept out of the thin slice — documented seams)

- **The per-claim interest-alignment judging pass** (LLM/expert-flagged against the pack's
  source-interest patterns, §9.1) — the Fact's `interest_alignment` is `None` until it runs.
- **Track-record belief revision** of source credibility after a refuted claim (§9.1) → Phase
  3/4; this increment computes point-in-time credibility from current inputs.
- **Independence-aware corroboration + coherence/triage** defenses (§9.1) compose *around*
  credibility in Phase 4, not inside the scalar.
- **`DERIVED_FROM` sensitivity propagation** to conclusions → Phase 3/5.
- **`significance` prior** is an *edge* property (SUPPORTS/REFUTES), so it lands with those
  edges in Phase 4; a Fact stores no significance — the box `reliability_prior` it would seed
  from is already reachable (Fact→Box).

### Tests

- **Unit:** `test_credibility.py` (the gate — observation interest-independent for every
  alignment; judgement discount/boost; clamp to [0,1]; UNKNOWN identity; out-of-range raise);
  `test_governance.py` (`Sensitivity.from_props` round-trip + JSON-string/absent/empty
  compartments; `InterestAlignment` vocab); `test_extract.py` (`seed_sensitivity` lub-fold;
  `fact_to_props` omits `interest_alignment` when unjudged, writes it when set).
- **Integration (`tests/integration/test_credibility.py`, live AGE):** a Fact inherits a
  confidential span's sensitivity (lub); `effective_credibility_of` returns the box reliability
  for an observation; for a judgement it passes reliability through while unjudged, then
  discounts once an alignment pass flags it self-serving. (Runs in CI.)

### Exit criteria (G2.6)

- [x] A base Fact's `sensitivity` is the lub of its source Span(s) (§9.1), not the lattice
      origin — seeded at extraction.
- [x] Effective credibility is **computed** from stored inputs (box reliability ×
      epistemic-class-gated interest modifier), never stored as a scalar; an observation's
      credibility is interest-independent, a judgement's is interest-weighted.
- [x] The per-claim `interest_alignment` input slot exists on the Fact (placeholder, `None`
      until the judging pass) and is read by the credibility computation.

---

## G2.5 — Part-whole abstraction levels *(shipped — thin slice)*

**Goal.** A fact attaches at a *level* of the domain's part-whole structure, and **level is
relative, derived, and a property of the referent — not the sentence** (§14): no stored level
scalar; a reasoning node's level is the position of its **subject-role** `INVOLVES` entity in
the `PART_OF` order. This increment builds that order over `Actor`/`Object` entities (typed,
split into intransitive `directPartOf` + the transitive `partOf` closure, roll-up restricted
to the transitivity-safe component-integral subtype, §14) and derives level from it.
Acquisition is **anchor-first, induce-fallback** (§14); since anchoring needs entity-linking
(the deferred G2.3/G2.4 seam), this slice ships the **induce path** — the §9.1 "induce-mode"
that is the correct cold-start behaviour, everything provisional.

### What shipped

- **`iknos/core/partwhole.py`.** Pure/DB split on the `core/reference.py` discipline:
  - *Pure (DB-free, unit-testable):* the detection schema (`_PartOfOut`/`InducedMeronymy`) +
    prompt; **`transitive_closure`** — the cycle-safe `directPartOf`→`partOf` closure
    (Kahn-peel to isolate any meronymy *cycle*, which is a contradiction excluded from
    roll-up and flagged, §14; then memoized DFS over the acyclic DAG); **`derived_level`**
    (partonomy depth = component-integral ancestor count — depth 0 coarsest, structure-only,
    *not* embedding cosine / lexical concreteness, §14); `_resolve_endpoints` (map detected
    surfaces to canonical entities, drop unresolved/self-loop); and the `directPartOf` /
    `partOf` write contracts.
  - *`MeronymyInducer`:* the operator (`actor="meronymy-inducer"`, `action_type="induce"`).
    Box-scoped, three-phase like the reference binder — Action-log idempotency → concurrent
    detection → serial `directPartOf` persist — then a final box-wide `partOf` closure
    recompute (restricted to component-integral via `edges.is_transitive`). `entity_level` /
    `fact_level` are the derived-level reads (a fact with several subject referents yields
    several levels — uncertain/multiple, never forced, §14).
- **`iknos/types/edges.py`** gained `MeronymyType` (Winston/Chaffin/Herrmann subtypes) +
  `is_transitive` (only component-integral) + `AttachmentProvenance`
  (`anchored`/`induced`/`relative`).

### Decisions

- **Typed and split; roll-up only along component-integral (§14).** `directPartOf` is each
  direct step; `partOf` is the closure, and `is_transitive` gates which subtype rolls up — a
  member-collection / portion-mass relation is recorded but **excluded** from `partOf`, so
  wrong aggregations never leak into coarse views. One definition of the rule
  (`_TRANSITIVE_MERONYMY`), read by the closure and any later view code.
- **Cycles are excluded and flagged, not closed through.** A meronymy cycle is a contradiction
  (no valid hierarchy); the closure isolates the cyclic nodes (Kahn) and excludes them, returning
  them as an unstable region for review — never a silent self-ancestor.
- **Level is derived, never stored (§14).** `entity_level`/`fact_level` *compute* depth from
  the live `partOf` order, so it stays correct as the hierarchy is refined; there is no `level`
  property on a Fact. The continuous intrinsic-IC refinement (Seco) and box-embedding/ConE
  generality for out-of-taxonomy entities are deferred seams — this slice is the depth term
  they scale.
- **Canonical-by-label endpoints + canonicalizing read.** `directPartOf` connects label-grouped
  canonical entities (the `reference.group_referents` representative), and the level read
  resolves a fact's subject node through the **same** grouping (`_canonical_map`) before
  counting ancestors — so any fresh node of an entity reports the same level, robust to whether
  entity resolution (G2.3) has run (its `SAME_AS` min-id representative coincides with this
  canonical; folding `SAME_AS` in is a later refinement).
- **Idempotency keyed on the proposition id**; the closure recompute is structurally idempotent
  (`merge_edge` upsert). Retraction/cleanup of stale `partOf` on edge removal is belief revision
  (Phase 3) — this slice's closure is monotonic per run.
- **No migration.** `directPartOf`/`partOf` exist (migration 0004); the 0007 indexes cover them.

### Deferred (kept out of the thin slice — documented seams)

- **Anchoring to the pack taxonomy** (the *primary*, reliable path, §14) — needs entity-linking
  → with G2.3/G2.4 anchor-canonicalization. This slice runs induce-mode (everything provisional).
- **Relative ordering (last resort)** — containment cues + co-occurrence/degree asymmetry + the
  §2 chunk prior when no parent is named (§14 step 3).
- **Continuous level / intrinsic IC + box-embedding/ConE** generality (§14); never embedding
  cosine or lexical concreteness.
- **Coverage-policy metric** (fraction of referents that anchor) — needs anchoring to exist.
- **Belief-revision / retraction** of induced edges + stale-`partOf` cleanup → Phase 3.
- **Merge with anchored structure / cross-pack taxonomy conflict resolution** → with anchoring.

### Tests

- **Unit (`tests/unit/test_partwhole.py`, DB-free):** `is_transitive` (only component-integral);
  `transitive_closure` (chain, diamond DAG, cycle exclusion+flag, self-loop drop, order
  independence); `_acyclic_edges` cycle separation; `derived_level` (ancestor count, distinct
  ancestors in a DAG); `_resolve_endpoints` (label mapping, unresolved/self-loop drop);
  detection-schema default; the `directPartOf`/`partOf` write contracts.
- **Integration (`tests/integration/test_partwhole.py`, live AGE):** a four-level
  component-integral chain across propositions → 3 `directPartOf` + a 6-pair `partOf` closure;
  a fact's `fact_level` derives the roller's depth (3) even though its subject is a *different*
  fresh node than the one in the hierarchy (canonicalization); idempotent re-run; a
  member-collection relation tagged but **excluded** from the `partOf` roll-up. (Runs in CI.)

### Exit criteria (G2.5)

- [x] `Actor`/`Object` entities form a typed `directPartOf` (step) + `partOf` (closure)
      hierarchy; roll-up runs only along the transitivity-safe component-integral subtype.
- [x] The hierarchy is a DAG: meronymy cycles are excluded from roll-up and flagged, never
      closed through.
- [x] A fact's abstraction level is **derived** from its subject-role referent's partonomy
      depth (uncertain/multiple when ambiguous), never a stored scalar.
- [x] Induced edges carry the meronymy type + `provenance=induced` + confidence + two
      annotations + bitemporal fields; re-inducing a box writes no duplicate edges.

---

## G2.8 — Entity-linking / taxonomy anchoring *(shipped — slice 1)*

**Goal.** Build the **anchor** mechanism (§5.2/§9/§14): entity-link a case box's
`Actor`/`Object` entities to the active domain pack(s)' curated taxonomy `Object` nodes,
recording each link as a scored, directed `ANCHORS_TO` edge. Anchoring is the *primary,
reliable* identity/level path that G2.3 (anchor-canonicalization), G2.4 (taxonomy-anchor
binding), and G2.5 (anchor-first levels, coverage policy) all defer to — and "the reliability
driver across domains" (§ phase risks): a domain works well exactly when its pack covers most
referents. **Slice 1** ships the linking subsystem **purely additively** — a new module, a new
edge label, no edits to the shipped `resolve`/`partwhole` — so the foundation lands reviewable
and un-regressing; **slice 2** wires the consumers (below).

### What shipped

- **`iknos/core/anchor.py`.** Pure/DB split on the `core/resolve.py` discipline:
  - *Pure (DB-free, unit-testable):* `TaxonomyNode` (a pack `Object` candidate); `block_anchors`
    (cheap lexical blocking — shared normalized tokens, **no** kind gate since a pack taxonomy
    is single-kind, so a mis-classified case `Actor` can still anchor); `score_anchor` (the
    deterministic lexical score — best-direction token containment + an exact-normalized-label
    bonus that alone reaches the confirm bar + a faint, never-disconfirming type bonus);
    `decide_anchor` (the conservative bars + tie handling); and **`anchors_to_props`** (the
    single canonical `ANCHORS_TO` write contract, cf. `resolve.same_as_to_props`).
  - *`EntityLinker`:* the operator (`actor="entity-linker"`, `action_type="anchor"`). Box-scoped
    like the resolver; `anchor_box` runs load (active-pack taxonomy + case entities as canonical
    referents) → block → score → decide → persist with one `anchor` Action, then commits.
    `anchored_targets` (the confirmed-anchor read slice-2 canonicalization consumes) and
    `coverage` (the §14 coverage-policy metric) are the reads.
- **`iknos/types/edges.py`** gained `AnchorState` (`candidate`/`confirmed`, mirroring
  `SameAsState`/`BindingState`) for the `ANCHORS_TO` edge.
- **Migration `0011_anchors_to_label`** creates the `ANCHORS_TO` elabel (the 0004 pattern) +
  the `start_id`/`end_id` endpoint btree indexes (the 0007 pattern) the per-entity link
  existence check and the coverage/`anchored_targets` reads traverse.

### Decisions

- **A dedicated `ANCHORS_TO` edge, not an overloaded `SAME_AS`.** Anchoring crosses boxes
  (case → reference pack) and is **directional** (case entity → taxonomy node), which encodes
  *anchor canonicalizes* (§5.2/§14): the taxonomy node is the authoritative identity. A peer
  `SAME_AS` would make the canonical merely the within-box min-id of a component — the wrong
  semantics — and §9 keeps cross-box identity out of the within-box resolution component. The
  separate label also keeps each subsystem's edge set independently queryable and indexable.
- **Deterministic, lexical, no LLM/embeddings** (the `resolve`/`reference` precedent). Anchoring
  scores on token containment + exact normalized-label match (the controlled-vocabulary signal);
  similarity is barred (§5.2 blocking-only), embedding cosine / lexical concreteness are the
  *wrong* level proxies (§14). So the whole pass is deterministic and structurally idempotent —
  re-anchoring an unchanged (box, taxonomy) upserts the same edges; the `anchor` Action is an
  audit record per run, not an idempotency key (cf. `resolve`).
- **Conservative under-anchor.** An exact label match to a *single* taxonomy node confirms;
  containment-only, or a tie between taxonomy nodes (a cross-pack homonym — a "valve" in two
  active packs), stays **open** as `CANDIDATE` edges for expert disambiguation (§ phase risks:
  cross-domain ambiguity is resolved by pack scope + review). An over-eager anchor
  mis-canonicalizes an entity and corrupts its derived level — the costlier failure.
- **Canonical-by-label endpoints** (the `partwhole` precedent). The case side of an anchor is the
  `reference.group_referents` canonical (label-grouped, min-id) entity, so an anchor is robust to
  whether entity resolution (G2.3) has run and the slice-2 level/identity reads canonicalize
  through the same grouping before following the anchor.
- **Type is a faint tie-breaker, never disconfirming.** Unlike `resolve` (both sides LLM-typed),
  the case `type` is a free-text guess and the taxonomy `type` is the pack `EntityType` name —
  they rarely string-match, so a mismatch must not block an otherwise-exact anchor (it would
  collapse coverage to ~0). Exact label alone confirms.
- **Coverage over canonical case entities.** `AnchorCoverage` = confirmed-anchored / total
  canonical entities — the §14 fraction that drives the pack-adequacy decision (high → anchoring
  is the level mechanism; low → induce + review + provisional levels).

### Deferred (slice 2 / later — documented seams)

- **Anchor-canonicalization fold** — `resolve.canonical_components` and the `partwhole` level
  read prefer a confirmed anchor as the canonical identity / level source (anchor-first level off
  the pack's `partOf` depth, stamping `AttachmentProvenance.ANCHORED`) → slice 2. Slice 1 writes
  the edges + exposes `anchored_targets`; it does **not** mutate the shipped `resolve`/`partwhole`
  behaviour, so nothing already-green regresses.
- **Taxonomy-anchor stage in the reference binder** (§3.1 cascade tail) — a `Mention` that fails
  the in-graph stage binds to a taxonomy node via the same linking → slice 2.
- **Embedding-neighbourhood blocking** (§5.2) — needs an entity-embedding store; slice 1 blocks
  lexically.
- **Belief-revision / retraction** of a stale anchor (re-run Layer A/B; clean superseded
  `ANCHORS_TO`) → Phase 3. Slice 1's edge set is monotonic per run.
- **Investigation-scoped pack activation** (§9) — slice 1 anchors against all active packs; the
  `pack_box_ids` parameter is the seam where the Phase-6 `ACTIVATES`-edge scope plugs in.

### Tests

- **Unit (`tests/unit/test_anchor.py`, DB-free):** blocking (shared-token, empty-label, no-overlap);
  scoring (exact reaches confirm, containment is candidate band, no-overlap zero, type bonus
  not-required / mismatch-not-disconfirming, partial-containment below candidate); decision
  (single-exact confirm, containment tie → candidate set, partial → candidate, no-candidate →
  unresolved, two-exact ambiguity → candidate); `anchors_to_props` shape (state/strength/two
  annotations/open bitemporal/`target_box`); `AnchorCoverage.fraction`.
- **Integration (`tests/integration/test_anchor.py`, live AGE):** load `pump_basic` → extract a
  case box (mocked LLM) with `roller` (exact → confirmed `Roller`), `pump` (contained in
  `Centrifugal pump` **and** `Pump housing` → tie → two candidates), and `gearbox` (out of
  taxonomy → no edge); assert the edges + states + annotations, `anchored_targets` (confirmed
  only), `coverage` (1 of 3) agreeing with the run-time coverage, the `entity-linker` Action
  joinable, and an idempotent re-run (no duplicate edges, same coverage). (Runs in CI; locally
  collected only when `DATABASE_URL` is set.)

### Exit criteria (G2.8 slice 1)

- [x] Case `Actor`/`Object` entities link to the active pack taxonomy via a scored, directed
      `ANCHORS_TO` edge through a cheap lexical cascade, box-scoped.
- [x] The default is conservative: `CONFIRMED` only on a single high-bar (exact) match, else
      bridgeable `CANDIDATE` edges (kept open on a cross-pack tie); an out-of-taxonomy entity
      writes no edge.
- [x] Anchoring is deterministic (no LLM/embeddings) and structurally idempotent; every run
      emits an `Action`.
- [x] The §14 coverage metric (confirmed-anchored / total) and the confirmed-anchor read
      (`anchored_targets`) ship as the slice-2 canonicalization/level consumers' inputs.

---

## G2.9 — Quarantine enforcement *(shipped)*

**Goal.** Make the §3.1 rule *enforceable*, not decorative: a **provisional** atom *"may exist but
must not drive a strong move (e.g. a `REFUTES` that overturns a hypothesis) until confirmed."* The
`provisional` flag has been set per proposition since Phase 1 (`is_provisional` /
`combine_faithfulness`), but until evidential edges existed there was no high-stakes move to gate.
G4.3 slice 3 landed the edge producer that writes `SUPPORTS`/`REFUTES` — so the gate lands now, at
edge-creation (the producer *marks*) and at adjudication (the QBAF adapter *honours*).

### What shipped

- **`iknos/core/quarantine.py` (pure, DB-free).** The one place the §3.1 decision lives, so the two
  consumers cannot diverge on what "high-stakes" means:
  - `QuarantinePolicy` — `high_stakes_signs: frozenset[EdgeSign]` (default `{REFUTES}`), a swappable
    data object (cf. `_BAND_LOWER_BOUNDS`, `DEFAULT_SIGNIFICANCE`); `is_high_stakes(sign)`.
  - `is_quarantined(sign, source_provisional, *, policy)` — `True` iff the source is provisional
    **and** the sign is high-stakes. Total over scalars, pure; a `null` provisional reads as
    `False` (quarantine fires only on a *positive* signal, never on absence of evidence).
- **`iknos/core/edge_producer.py` — the producer *marks*.**
  - `NodeMeta.provisional` (the node's own flag — a `Conclusion` carries it; `_load_node_meta`
    reads `n.provisional`).
  - `_load_provisional(session, evidence_ids, node_meta)` — resolves each evidence node's
    provisional status: OR-folds the node's own `provisional` (an `induce`d conclusion) with the
    base-Fact perception gate (`Fact -[:EVIDENCED_BY]-> Proposition.provisional`).
  - `evidential_edge_props(..., quarantined)`, `plan_hypothesis(..., provisional, quarantine_policy)`,
    `_edge_audit(..., quarantined)`, `ProducedEdge.quarantined`, `EdgeProductionResult.quarantined`
    (the persisted quarantined edges — the Phase-7 triage queue's input). `EdgeProducer` takes a
    `quarantine_policy` (default `DEFAULT_QUARANTINE`).
- **`iknos/core/qbaf_adapter.py` — the adapter *honours*.** `EvidenceRow.quarantined`,
  `_load_evidential_edges` reads `r.quarantined`, and `assemble_baf` **drops** a quarantined edge
  from the framework (it lends nothing to a hypothesis's state) — exactly as a dead-endpoint edge is
  dropped. This is the actual enforcement: without it the marked `REFUTES` would still attack.

### Decisions

- **Mark-and-drop, not block-the-write.** §3.1 says the edge *"may exist but must not drive"* — so
  the producer persists a quarantined `REFUTES` (auditable; the votes + `quarantined` are in the
  Action) and the adapter excludes it. The edge **lifts automatically** on confirmation: when the
  source's `provisional` clears, a re-judgment's `merge_edge` (full `SET r = {…}`) overwrites the
  flag, and the QBAF re-includes it. (Belief-revision re-run on a flip is the Phase-3 seam; the
  re-judgment path covers the simple case today.)
- **The edge layer marks; it does not adjudicate.** This mirrors the existing `sign_stable`
  contract: the producer surfaces a first-class graph-queryable signal, and the gate (the QBAF /
  the §7.2 ensemble, G4.5) consumes it. Quarantine is the *perception-layer* analogue of the §7.2
  *ensemble-layer* gate on refutation — both protect a hypothesis from being overturned on weak
  grounds, at two different layers.
- **Categorical sign gate now; stakes-dependent cutoff deferred.** §3.1 names the move categorically
  (a `REFUTES`) and also says the cutoff is *stakes-dependent* (a high-significance refutation needs
  higher source faithfulness than a minor one). The categorical rule — the one that actually
  protects hypothesis state — ships; the continuous faithfulness-vs-significance threshold is a
  genuine calibration question (Trial A5 / G4.6) left as the documented `QuarantinePolicy` seam
  rather than guessed.
- **`SUPPORTS` is not gated.** Corroboration is a low-stakes move; a provisional source's weaker
  support is already expressed by its lower edge `strength` / node `confidence`, so a hard gate
  would double-count. Only the named high-stakes move is gated.
- **Backward-compatible read.** A `quarantined` property absent (pre-G2.9 edges) or `null` reads as
  not-quarantined (`_flag` / `_opt_bool`), so an existing graph adjudicates exactly as before until
  its edges are re-judged.

### Deferred (documented seams)

- The **stakes-dependent continuous threshold** (Trial A5 / G4.6) — a `QuarantinePolicy` field +
  richer `is_quarantined` signature, swapped in without touching either consumer.
- **Expert-triage routing** of quarantined edges (Phase 7) — `EdgeProductionResult.quarantined`
  surfaces them; the queue that confirms the provisional source (lifting the quarantine) is Phase 7.
- **Belief-revision re-run** when a confirmation lifts a quarantine (Phase 3) — today the lift
  happens on the next `produce` re-judgment, not on an incremental Layer A/B re-run.

### Tests

- **Unit (`tests/unit/test_quarantine.py`, DB-free):** the categorical rule (provisional → `REFUTES`
  gated, `SUPPORTS` allowed), the non-provisional pass-through (both signs), the default policy's
  high-stakes set, and a custom policy extending it (the swappable seam).
- **Unit (`tests/unit/test_edge_producer.py`):** `plan_hypothesis` quarantines a provisional
  source's `REFUTES` but not its `SUPPORTS` nor a non-provisional `REFUTES`; the Action audit
  carries `quarantined`; `evidential_edge_props` carries the flag; `EdgeProductionResult.quarantined`
  surfaces it; the end-to-end `produce` writes `quarantined=True` for a provisional source's refute.
- **Unit (`tests/unit/test_qbaf_adapter.py`):** `assemble_baf` drops a quarantined edge from the
  framework while keeping the source an argument and a sibling non-quarantined attack live.
- **Integration (`tests/integration/test_edge_producer.py`, live AGE):** a `Fact` whose source
  `Proposition` is provisional drives a `REFUTES`; the written edge is `quarantined=True`, the
  result surfaces it, and `QbafAdapter.evaluate` leaves the hypothesis at its base acceptability
  (the provisional refuter did not overturn it). (Runs in CI; locally collected only when
  `DATABASE_URL` is set.)

### Exit criteria (G2.9)

- [x] A provisional atom (low-faithfulness/ambiguously-bound proposition, or a defeasible inductive
      conclusion) cannot drive a `REFUTES`: the edge producer marks the edge `quarantined` and the
      QBAF adapter drops it, so the move does not overturn a hypothesis until the source is confirmed.
- [x] The quarantine is enforced **at edge-creation time** (the §3.1 / Phase-2-entry-criterion
      wording) and is graph-queryable (`r.quarantined`) and auditable (the producing `Action`).
- [x] A `SUPPORTS` from a provisional source is **not** gated (low-stakes corroboration).
- [x] The quarantine lifts non-destructively when the source is confirmed (re-judgment overwrites
      the flag); the edge is never silently dropped from the graph.
