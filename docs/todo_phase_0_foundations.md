# Phase 0 — Foundations & Data Model

> **Status: COMPLETE.** All exit criteria met; the schema-widening gaps are tracked
> and closed in `archive/gap_phase_0_foundations.md` (G0.1–G0.8, merged PRs #11–#15), and the
> post-merge review fixes in `archive/gap_phase_0_residual.md` (G0.R1, PR #16; G0.R2 — AGE
> label indexes, migration 0007 — a Phase 2 entry criterion). The single
> remaining unchecked item — `WITH RECURSIVE` + SCC detection — is **intentionally
> deferred to Phase 3** (no consumer yet; the requirement is recorded on the Phase 3
> reachability helper, see G0.8). Next work is Phase 1 (`archive/gap_phase_1_ingest.md`,
> starting G1.9 span persistence). Pydantic node projections for not-yet-needed labels
> (Actor/Object/Hypothesis/Task/Mention) are deferred to their consuming phases by the
> project's node-projection convention — the *label + property contract* is what Phase 0
> fixes.

**Goal:** a running single-engine store and the authoritative schema contract, with
provenance and audit plumbing in place from the start. Everything else builds on this.

**Depends on:** nothing (first phase).
**Architecture refs:** §6 (storage), §10 (schema), §9 (boxes/tiers — registry), §10.1
(action log), principles 4, 6, 7, 9.

## Project scaffolding

- [x] Initialize repo with the module split: `types/`, `core/`, `operators/`, `api/`,
      `app/` (§6).
- [x] Dev environment: containerized PostgreSQL with **Apache AGE** and **pgvector**
      extensions; one instance, one graph (§6).
- [x] Dependency/license tracking; confirm the open-source stack is self-hostable
      (principle 7).
- [x] CI skeleton + test harness; reserve a fixture-corpus location for later phases.
      (CI = ruff gate + live-DB pytest + up/down/up migration drift gate.)

## Storage engine

- [x] Provision Postgres + AGE + pgvector; verify the AGE property graph and relational
      tables live in the same instance (local-join provenance, §10 resolution rule).
- [x] Create the single AGE graph; confirm box partitioning will be logical (a `box`
      property), not separate graphs (§9).
- [ ] Set up `WITH RECURSIVE` patterns for transitive reachability and **SCC detection**
      over `DERIVED_FROM` (well-founded-support retraction and cycle-safe handling later
      rely on this, §12). **→ Deferred to Phase 3** (no consumer in Phase 0; requirement
      recorded on the Phase 3 reachability helper, see `archive/gap_phase_0_foundations.md` G0.8).

## Schema contract (§10) — the authoritative data model

- [x] Node labels with properties: `Document`, `Span`, `Proposition`, `Mention`,
      `Actor`, `Object`, `Fact`, `DeductiveConclusion`, `InductiveConclusion`,
      `Hypothesis`, `Task`, `Box`. (Labels created by migrations `0001`+`0004`;
      `Mention`/`Task` added in G0.2. Pydantic projections land per consuming phase.)
- [x] Edge types: `EVIDENCED_BY`, `INVOLVES` (with `role`), `DERIVED_FROM`,
      `SUPPORTS`/`REFUTES` (carry `sign`, `strength`, `significance`), `RELATES`,
      `REFERS_TO` (Mention→entity, scored, §3.1), `SAME_AS` (entity identity, scored,
      candidate/confirmed, §5.2), `PART_OF` — typed and split: `directPartOf`
      (intransitive, each decomposition step) and `partOf` (its transitive closure), with
      a meronymy-type tag; roll-up restricted to the component-integral subtype (§14);
      and the intentional-layer edges `DECOMPOSES_INTO`, `ADDRESSES`, `RELEVANT_TO`
      (§11.2). (`INVOLVES.role` = G0.4; identity/part-whole/intentional elabels = G0.3.)
- [x] **Intentional layer (§11.2):** `Task` (investigative goal/question — `type`,
      `answer_state`; *answered*, not adjudicated true/false — distinct from epistemic
      nodes). Hypothesis `acceptability` bands to true/plausible/implausible/false for
      presentation. (G0.5 — `types/intentional.py`: `TaskType`/`AnswerState`/
      `HypothesisState`/`AcceptabilityBand` + `band()`.)
- [x] Abstraction **level is derived, not stored**: a node's level = its subject-role
      `INVOLVES` entity's position in the `partOf` order (§14). Optionally materialize
      a depth/rank for query performance, recomputed when the hierarchy changes. (G0.4 —
      documented on `Role`; depth/rank is a forward Phase 6 cache, no Phase 0 code.)
- [x] Property conventions: every reasoning node/edge carries `box` and (where it
      reasons) `tier`; tier inherited from `Box`, override allowed. (`Tier` =
      schema/reference/case/working, G0.1.)
- [x] **Two-annotation rule baked into the schema:** integer support-count (Layer A)
      and `[0,1]` confidence (Layer B) on facts/edges (§12). Document that they are
      never collapsed. (`types/annotations.py::Annotations`.)
- [x] **Governance attributes (§9.1):** `sensitivity` (lattice label + compartment tags,
      propagated to derived nodes as the max of antecedents); source `interest`/role and
      conditional `credibility` (base reliability × claim-interest alignment), distinct
      from faithfulness (§3.1) and edge strength (§8). Define the sensitivity lattice.
      (G0.6 — `types/governance.py`: `Sensitivity`/`lub` + `SourceInterest`; credibility
      derived-not-stored. Propagation walk deferred to the governance track.)
- [x] Bitemporal fields on claims and evidential edges: `event_time`, `ingested_at`,
      `valid_from`, `valid_to` (fields now; supersession logic in Phase 5) (§7.4).
      (`types/temporal.py::BitemporalFields`.)
- [x] `override` property placeholder on reasoning nodes/edges (logic in Phase 7,
      §10.3).
- [x] Relational tables: raw text + offsets keyed by `Document.id`; pgvector table for
      embeddings; join-by-id to the graph. (`db/orm.py`.)

## Provenance & audit plumbing (must exist now, not later)

- [x] `Span` as the sole provenance reference; implement `Span → (document_id, start,
      end) →` text resolution as a local join (§10 resolution rule). (`db/spans.py::
      resolve_span_text`.)
      **Schema addition (revised plan, §1/§10):** `Span` also carries an optional
      `layout {page, bbox}` for *visual* provenance (claim → region on the original
      page image). The field's only consumer is the Phase-1 parse front-end, so the
      `types/nodes.py::Span` + ORM field-add is tracked with it — `archive/gap_phase_1_ingest.md`
      G1.0 / widened G1.9 — not retrofitted here.
- [x] **Process action log** (`Action` table, append-only, §10.1): `id`, `timestamp`,
      `actor`, `action_type`, `inputs`, `outputs`, and the LLM fields (`model`,
      `sampling`, raw judgment, calibration) — schema and write-path ready for
      operators to use from Phase 2. (`provenance/action_log.py`, `db/orm.py::Action`.)
- [x] Box registry (`(:Box)` node): `tier`, `version`, `source`, `reliability_prior`,
      `valid_from`, `valid_to`, `status` (§9). (`types/nodes.py::Box`.)
- [x] **Epistemic vs domain schema split (§9):** the node/edge types above are the
      *fixed epistemic schema* (domain-agnostic). Reserve the *domain layer* — entity
      types, part-whole taxonomy, domain rules — as pluggable, supplied by domain packs.
      (G0.7 — `src/iknos/domain/`.)
- [x] **Domain pack** scaffold: a domain pack = reference/schema-tier box(es) bundling
      a part-whole taxonomy + entity-type ontology + optional rules. Define how a pack
      is declared, versioned, and activated per investigation (§9). At least one trivial
      pack loadable end-to-end. (G0.7 — `domain/{pack,loader}.py`, `packs/pump_basic.json`;
      hardened immutable-per-version in G0.R1. Per-investigation activation is a Phase 6
      seam — `Box.status` is the activation flag for now.)

## Exit criteria

- [x] A document and a span can be stored, and text resolved back from a span by id.
- [x] A node and an edge can be created carrying box, tier, both annotations, and
      bitemporal fields.
- [x] An `Action` record can be written and linked to the node/edge it produced.
- [x] The schema is documented in code as the single contract; matches `architecture.md`
      §10. (Exercised by `tests/integration/test_phase_0_exit_criteria.py`.)

## Phase risks / decisions

- AGE's openCypher is partial — validate the actual query patterns (neighbor fetch,
  box-scoped traversal, recursive closure) early; fall back to SQL where needed.
- Lock naming/ID conventions now; downstream phases assume them.

## Build record *(merged from `archive/gap_phase_0_foundations.md` / `archive/gap_phase_0_residual.md`, 2026-06-11; full rationale in `docs/archive/`)*

All Phase 0 gaps closed (PRs #11–#16, #42). One-line record per item:

- **G0.1** — `Tier` renamed to `schema/reference/case/working` (§9/§10), code-only.
- **G0.2/G0.3** — `Mention` + `Task` vlabels and `REFERS_TO`, `SAME_AS`,
  `directPartOf`/`partOf` (meronymy-typed, split per §14), `DECOMPOSES_INTO`,
  `ADDRESSES`, `RELEVANT_TO` elabels (migration `0004`).
- **G0.4** — `INVOLVES.role` (`Role` StrEnum); abstraction level is **derived, not
  stored** (any depth/rank materialization is a cache, never authoritative).
- **G0.5** — `AcceptabilityBand` + pure `band()` and `AnswerState`/`TaskType` in
  `types/intentional.py`; Task is *answered*, Hypothesis is *adjudicated*.
- **G0.6** — `sensitivity` (lattice + compartments, max-propagation rule documented)
  and `Box.source_interest`; credibility is derived-not-stored (§9.1).
- **G0.7** — domain-pack scaffold (`domain/{pack,loader}.py`, `packs/pump_basic.json`).
- **G0.8** — `WITH RECURSIVE` + SCC detection deferred to Phase 3 with the
  cycle-safety requirement recorded there (landed as G3.2 DRed).
- **G0.R1** — packs immutable per version (`content_hash`, `PackImmutabilityError`);
  `valid_from` is create-only on every box-write path (one shared serde,
  `boxes/serde.py`, so the two box-write paths cannot diverge).
- **G0.R2** — AGE label indexes (migration `0007`): **GIN on `properties`** per vertex
  label + **btree on edge `start_id`/`end_id`**. The originally-proposed btree
  expression indexes were overturned by `EXPLAIN`: AGE compiles property-map filters
  to `@>` containment, which GIN serves and the expression index never would. The
  verification test asserts index *use* through the real `cypher()` path, not
  existence (`tests/integration/test_age_label_indexes.py`).

**Reviewed and dismissed — do not re-raise** *(each was checked against code and
confirmed not to realize; full analysis in `docs/archive/gap_phase_0_residual.md`)*:
`SensitivityLevel` alphabetical `<` (ordering goes through `_SENSITIVITY_RANK`/`lub`,
pinned by a characterization test); Cypher injection in `loader.py` (all free text
flows through `cypher_map`; raw slots are UUID/ISO/constant only); `_merge_edge`
endpoint no-op (caller MERGEs endpoints in the same transaction); `entity_types` as a
JSON string property (round-trips; consumer parses); part-whole edges carrying only
`valid_from` (contract requires the full quad only on `REFERS_TO`/`SAME_AS`);
`HypothesisState` 3-way vs `AcceptabilityBand` 4-way (reconciled when Phase 4 wired
the QBAF); `cypher_map` float formatting (revisit if a path persists floats outside
`[0,1]` — Phase 4 persists `strength`/`significance`, both in `[0,1]`); pack-version
loading not deprecating the old version (intended: per-investigation activation).
