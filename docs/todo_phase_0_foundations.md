# Phase 0 — Foundations & Data Model

**Goal:** a running single-engine store and the authoritative schema contract, with
provenance and audit plumbing in place from the start. Everything else builds on this.

**Depends on:** nothing (first phase).
**Architecture refs:** §6 (storage), §10 (schema), §9 (boxes/tiers — registry), §10.1
(action log), principles 4, 6, 7, 9.

> **Status (reviewed 2026-06-02):** Substantially complete — all four exit criteria are
> implemented and exercised by `tests/integration/test_phase_0_exit_criteria.py`. Three
> items are carried forward (each annotated `→ deferred` below): the `WITH RECURSIVE`
> reachability helper (needed in Phase 3), the pgvector embeddings table (Phase 1 §1),
> and wiring the test suite into CI. **Caveat:** the integration test requires a live
> AGE DB and the host forbids `docker compose up`, so the exit-criteria test is written
> but has **not yet been executed** here — only the `migrations` CI (up/down/up + drift)
> runs against a live DB. Treat the exit criteria as *coded, pending a verified run*.

## Project scaffolding

- [x] Initialize repo with the module split: `types/`, `core/`, `operators/`, `api/`,
      `app/` (§6). *(`src/iknos/{types,core,operators,api}` present; `app/` is a
      documented deferred stub for the Phase 7 expert frontend.)*
- [x] Dev environment: containerized PostgreSQL with **Apache AGE** and **pgvector**
      extensions; one instance, one graph (§6). *(`compose.yaml` +
      `docker/postgres.Dockerfile`.)*
- [ ] **Partial.** Dependency/license tracking; confirm the open-source stack is
      self-hostable (principle 7). *(Stack is fully open-source and self-hostable; deps pinned in
      `pyproject.toml`/`uv.lock`. **No `LICENSE` file or formal license inventory yet**
      → deferred to the licensing cross-cutting track.)*
- [x] CI skeleton + test harness; reserve a fixture-corpus location for later phases.
      *(`tests/{unit,integration}` + `tests/fixtures/corpus/`; `.github/workflows/migrations.yml`.
      **CI runs only the migration up/down/up + drift check — it does not yet run
      pytest** → see deferred item under Exit criteria.)*

## Storage engine

- [x] Provision Postgres + AGE + pgvector; verify the AGE property graph and relational
      tables live in the same instance (local-join provenance, §10 resolution rule).
      *(Migration `0001_initial` creates extensions, graph, and relational tables in one
      instance; `resolve_span_text` does the local join.)*
- [x] Create the single AGE graph; confirm box partitioning will be logical (a `box`
      property), not separate graphs (§9). *(Graph `iknos`; `box` is a property on
      reasoning nodes/edges.)*
- [ ] Set up `WITH RECURSIVE` patterns for transitive reachability (retraction closure
      later relies on this, §6). **→ deferred to Phase 3** — no consumer exists yet;
      the retraction-closure walk is the first user. Not implemented in Phase 0.

## Schema contract (§10) — the authoritative data model

- [x] Node labels with properties: `Document`, `Span`, `Proposition`, `Actor`,
      `Object`, `Fact`, `DeductiveConclusion`, `InductiveConclusion`, `Hypothesis`,
      `Box`. *(All 10 vlabels pre-created in `0001_initial`. Pydantic projections exist
      for the Phase-0 minimal set — Document, Span, Box, Fact — with the rest
      intentionally deferred to their owning phases, documented in `types/nodes.py`.)*
- [x] Edge types: `EVIDENCED_BY`, `INVOLVES` (with `role`), `DERIVED_FROM`,
      `SUPPORTS`/`REFUTES` (carry `sign`, `strength`, `significance`), `RELATES`.
      *(All 6 elabels pre-created; `EvidencedBy` + `EvidentialEdge` Pydantic models
      carry sign/strength/significance. `INVOLVES.role` lands with the Actor/Object
      models in Phase 2.)*
- [x] Property conventions: every reasoning node/edge carries `box` and (where it
      reasons) `tier`; tier inherited from `Box`, override allowed. *(`Fact`,
      `EvidentialEdge` carry `box`+`tier`; `Tier` enum in `types/nodes.py`.)*
- [x] **Two-annotation rule baked into the schema:** integer support-count (Layer A)
      and `[0,1]` confidence (Layer B) on facts/edges (§12). Document that they are
      never collapsed. *(`types/annotations.py` — `Annotations(support_count:int,
      confidence:float)`, never-collapsed rule documented in the module docstring.)*
- [x] Bitemporal fields on claims and evidential edges: `event_time`, `ingested_at`,
      `valid_from`, `valid_to` (fields now; supersession logic in Phase 5) (§7.4).
      *(`types/temporal.py::BitemporalFields`.)*
- [x] `override` property placeholder on reasoning nodes/edges (logic in Phase 7,
      §10.3). *(`override: dict | None` on `Fact` and `EvidentialEdge`.)*
- [ ] **Partial.** Relational tables: raw text + offsets keyed by `Document.id`; pgvector
      table for embeddings; join-by-id to the graph. *(`document_content` (raw text) + `actions`
      done; span offsets live on `Span` graph nodes joined by id. **The pgvector
      embeddings table is NOT created** — the `vector` extension is enabled but the
      table itself is Phase 1 §1 (embedding substrate) → deferred to Phase 1.)*

## Provenance & audit plumbing (must exist now, not later)

- [x] `Span` as the sole provenance reference; implement `Span → (document_id, start,
      end) →` text resolution as a local join (§10 resolution rule).
      *(`db/spans.py::resolve_span_text`.)*
- [x] **Process action log** (`Action` table, append-only, §10.1): `id`, `timestamp`,
      `actor`, `action_type`, `inputs`, `outputs`, and the LLM fields (`model`,
      `sampling`, raw judgment, calibration) — schema and write-path ready for
      operators to use from Phase 2. *(`db/orm.py::Action` + `provenance/action_log.py::record_action`.)*
- [x] Box registry (`(:Box)` node): `tier`, `version`, `source`, `reliability_prior`,
      `valid_from`, `valid_to`, `status` (§9). *(`types/nodes.py::Box` with all fields;
      created + queried in the exit-criteria test.)*

## Exit criteria

- [x] A document and a span can be stored, and text resolved back from a span by id.
- [x] A node and an edge can be created carrying box, tier, both annotations, and
      bitemporal fields.
- [x] An `Action` record can be written and linked to the node/edge it produced.
- [x] The schema is documented in code as the single contract; matches `architecture.md`
      §10.

> All four are implemented and asserted in `tests/integration/test_phase_0_exit_criteria.py`.
> **Not yet verified by an actual run** — the test needs a live AGE DB (`DATABASE_URL`)
> and the host forbids `docker compose up`. **→ deferred:** (a) wire a `tests` CI job
> that brings up the AGE image (as `migrations.yml` does) and runs pytest, so the
> exit-criteria test executes on every change; (b) or get one-off approval to bring up
> postgres locally for a single verifying run. Until then the criteria are *coded,
> pending green*.

## Phase risks / decisions

- AGE's openCypher is partial — validate the actual query patterns (neighbor fetch,
  box-scoped traversal, recursive closure) early; fall back to SQL where needed.
  *(Partly validated: CREATE/MATCH/RETURN exercised in the exit-criteria test;
  box-scoped traversal and recursive closure remain unproven until Phase 2/3.)*
- Lock naming/ID conventions now; downstream phases assume them. *(Done: UUID `id` on
  every node, `box`/`tier` property names, snake_case relational columns, `Action`
  actor/action_type strings.)*

## Carried into later phases (open Phase 0 line items)

- **`WITH RECURSIVE` transitive-reachability helper** → Phase 3 (retraction closure is
  its first consumer).
- **pgvector embeddings table** → Phase 1 §1 (embedding substrate owns the schema).
- **`tests` CI job running pytest against a live AGE image** → so the Phase 0
  exit-criteria test (and later integration tests) actually execute. Currently only the
  `migrations` workflow touches a live DB.
- **`LICENSE` file + dependency license inventory** → licensing cross-cutting track.
