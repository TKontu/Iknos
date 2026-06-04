# Phase 0 — Foundations & Data Model

**Goal:** a running single-engine store and the authoritative schema contract, with
provenance and audit plumbing in place from the start. Everything else builds on this.

**Depends on:** nothing (first phase).
**Architecture refs:** §6 (storage), §10 (schema), §9 (boxes/tiers — registry), §10.1
(action log), principles 4, 6, 7, 9.

> **Status (updated 2026-06-04):** **Complete.** All four exit criteria are implemented
> and asserted in `tests/integration/test_phase_0_exit_criteria.py`, and that test now
> **runs green against a live AGE+pgvector DB** — both manually and on every push via the
> `tests` CI workflow (`.github/workflows/tests.yml`). The earlier "coded, pending a
> verified run" caveat is resolved.
>
> The first live run surfaced two latent DB-layer bugs that the schema-only `migrations`
> CI could never have caught — `db/age.py` (Cypher `:Label` misparsed as a SQLAlchemy
> bind param) and `db/spans.py` (`substring(... FROM ... FOR ...)` failing asyncpg type
> inference); both fixed (see `CI_MIGRATIONS.md` → "Runtime query-layer gotchas"). The
> pgvector embeddings table also landed (migration `0002`, via Phase 1 Increment 1).
>
> **Only two line items carry forward**, both genuinely owned by later phases: the
> `WITH RECURSIVE` reachability helper (→ Phase 3) and `LICENSE`/dependency-license
> tracking (→ licensing cross-cutting track).

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
      *(`tests/{unit,integration}` + `tests/fixtures/corpus/`. Two workflows:
      `.github/workflows/migrations.yml` (Alembic up/down/up + drift) and
      `.github/workflows/tests.yml` (builds the AGE+pgvector image and runs the full
      pytest suite, incl. live-DB integration, on every push).)*

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
- [x] Relational tables: raw text + offsets keyed by `Document.id`; pgvector
      table for embeddings; join-by-id to the graph. *(`document_content` (raw text) +
      `actions` from `0001`; span offsets live on `Span` graph nodes joined by id; the
      pgvector `document_embeddings` table landed in `0002` (Phase 1 Increment 1).)*

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

> All four are implemented and asserted in `tests/integration/test_phase_0_exit_criteria.py`,
> **verified green against a live AGE+pgvector DB** — manually, and on every push via the
> `tests` workflow (which builds the AGE image, migrates it, and runs the suite). The
> conftest hard-fails under CI if `DATABASE_URL` is unset, so the live-DB tests can never
> silently skip to a false green.

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
  its first consumer; no consumer exists yet).
- **`LICENSE` file + dependency license inventory** → licensing cross-cutting track.

*(Resolved since the 2026-06-02 review: pgvector embeddings table — `0002`; `tests` CI
job running pytest against live AGE — `tests.yml`; exit-criteria test verified green.)*
