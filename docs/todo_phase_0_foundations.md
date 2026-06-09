# Phase 0 — Foundations & Data Model

**Goal:** a running single-engine store and the authoritative schema contract, with
provenance and audit plumbing in place from the start. Everything else builds on this.

**Depends on:** nothing (first phase).
**Architecture refs:** §6 (storage), §10 (schema), §9 (boxes/tiers — registry), §10.1
(action log), principles 4, 6, 7, 9.

## Project scaffolding

- [ ] Initialize repo with the module split: `types/`, `core/`, `operators/`, `api/`,
      `app/` (§6).
- [ ] Dev environment: containerized PostgreSQL with **Apache AGE** and **pgvector**
      extensions; one instance, one graph (§6).
- [ ] Dependency/license tracking; confirm the open-source stack is self-hostable
      (principle 7).
- [ ] CI skeleton + test harness; reserve a fixture-corpus location for later phases.

## Storage engine

- [ ] Provision Postgres + AGE + pgvector; verify the AGE property graph and relational
      tables live in the same instance (local-join provenance, §10 resolution rule).
- [ ] Create the single AGE graph; confirm box partitioning will be logical (a `box`
      property), not separate graphs (§9).
- [ ] Set up `WITH RECURSIVE` patterns for transitive reachability and **SCC detection**
      over `DERIVED_FROM` (well-founded-support retraction and cycle-safe handling later
      rely on this, §12).

## Schema contract (§10) — the authoritative data model

- [ ] Node labels with properties: `Document`, `Span`, `Proposition`, `Mention`,
      `Actor`, `Object`, `Fact`, `DeductiveConclusion`, `InductiveConclusion`,
      `Hypothesis`, `Task`, `Box`.
- [ ] Edge types: `EVIDENCED_BY`, `INVOLVES` (with `role`), `DERIVED_FROM`,
      `SUPPORTS`/`REFUTES` (carry `sign`, `strength`, `significance`), `RELATES`,
      `REFERS_TO` (Mention→entity, scored, §3.1), `SAME_AS` (entity identity, scored,
      candidate/confirmed, §5.2), `PART_OF` — typed and split: `directPartOf`
      (intransitive, each decomposition step) and `partOf` (its transitive closure), with
      a meronymy-type tag; roll-up restricted to the component-integral subtype (§14);
      and the intentional-layer edges `DECOMPOSES_INTO`, `ADDRESSES`, `RELEVANT_TO`
      (§11.2).
- [ ] **Intentional layer (§11.2):** `Task` (investigative goal/question — `type`,
      `answer_state`; *answered*, not adjudicated true/false — distinct from epistemic
      nodes). Hypothesis `acceptability` bands to true/plausible/implausible/false for
      presentation.
- [ ] Abstraction **level is derived, not stored**: a node's level = its subject-role
      `INVOLVES` entity's position in the `partOf` order (§14). Optionally materialize
      a depth/rank for query performance, recomputed when the hierarchy changes.
- [ ] Property conventions: every reasoning node/edge carries `box` and (where it
      reasons) `tier`; tier inherited from `Box`, override allowed.
- [ ] **Two-annotation rule baked into the schema:** integer support-count (Layer A)
      and `[0,1]` confidence (Layer B) on facts/edges (§12). Document that they are
      never collapsed.
- [ ] **Governance attributes (§9.1):** `sensitivity` (lattice label + compartment tags,
      propagated to derived nodes as the max of antecedents); source `interest`/role and
      conditional `credibility` (base reliability × claim-interest alignment), distinct
      from faithfulness (§3.1) and edge strength (§8). Define the sensitivity lattice.
- [ ] Bitemporal fields on claims and evidential edges: `event_time`, `ingested_at`,
      `valid_from`, `valid_to` (fields now; supersession logic in Phase 5) (§7.4).
- [ ] `override` property placeholder on reasoning nodes/edges (logic in Phase 7,
      §10.3).
- [ ] Relational tables: raw text + offsets keyed by `Document.id`; pgvector table for
      embeddings; join-by-id to the graph.

## Provenance & audit plumbing (must exist now, not later)

- [ ] `Span` as the sole provenance reference; implement `Span → (document_id, start,
      end) →` text resolution as a local join (§10 resolution rule).
- [ ] **Process action log** (`Action` table, append-only, §10.1): `id`, `timestamp`,
      `actor`, `action_type`, `inputs`, `outputs`, and the LLM fields (`model`,
      `sampling`, raw judgment, calibration) — schema and write-path ready for
      operators to use from Phase 2.
- [ ] Box registry (`(:Box)` node): `tier`, `version`, `source`, `reliability_prior`,
      `valid_from`, `valid_to`, `status` (§9).
- [ ] **Epistemic vs domain schema split (§9):** the node/edge types above are the
      *fixed epistemic schema* (domain-agnostic). Reserve the *domain layer* — entity
      types, part-whole taxonomy, domain rules — as pluggable, supplied by domain packs.
- [ ] **Domain pack** scaffold: a domain pack = reference/schema-tier box(es) bundling
      a part-whole taxonomy + entity-type ontology + optional rules. Define how a pack
      is declared, versioned, and activated per investigation (§9). At least one trivial
      pack loadable end-to-end.

## Exit criteria

- [ ] A document and a span can be stored, and text resolved back from a span by id.
- [ ] A node and an edge can be created carrying box, tier, both annotations, and
      bitemporal fields.
- [ ] An `Action` record can be written and linked to the node/edge it produced.
- [ ] The schema is documented in code as the single contract; matches `architecture.md`
      §10.

## Phase risks / decisions

- AGE's openCypher is partial — validate the actual query patterns (neighbor fetch,
  box-scoped traversal, recursive closure) early; fall back to SQL where needed.
- Lock naming/ID conventions now; downstream phases assume them.
