# Gap Plan — Phase 0 (Foundations & Data Model)

> **Status: all gaps closed.** G0.1–G0.7 merged (PRs #11–#15); G0.8 deferred to
> Phase 3 by design (note recorded). A post-merge review then found and fixed one
> residual defect — see `gap_phase_0_residual.md` (G0.R1, PR #16). Phase 0 is
> complete; next work is Phase 1 (`gap_phase_1_ingest.md`, G1.9 first).

**Why this file exists.** Phase 0 was built against the *previous* plan and is
substantially complete (storage engine, schema contract, provenance + audit
plumbing; exercised by `tests/integration/test_phase_0_exit_criteria.py`). The
revised plan (`todo_phase_0_foundations.md` + `architecture.md` §9, §9.1, §10,
§11.2, §14) **widens the schema contract**. This plan lists the concrete code
revisions to bring the implemented foundations in line with it. It does not
re-list what already matches.

**Refs:** §6 (storage), §9 (tiers/boxes), §9.1 (governance), §10 (schema),
§11.2 (intentional layer), §14 (part-whole). Principles 4, 6, 7, 9.

## Current implementation (baseline)

- **AGE schema** (`alembic/versions/20260601_0001_initial_schema.py`):
  - vlabels: `Document, Span, Proposition, Actor, Object, Fact,
    DeductiveConclusion, InductiveConclusion, Hypothesis, Box`
  - elabels: `EVIDENCED_BY, INVOLVES, DERIVED_FROM, SUPPORTS, REFUTES, RELATES`
- **Pydantic schema** (`src/iknos/types/`): `Tier{AXIOM,DOMAIN,EVIDENCE,DERIVED}`,
  `BoxStatus`, `Document`, `Span`, `Proposition`, `Box`, `Fact` (`nodes.py`);
  `EdgeSign`, `EvidencedBy`, `EvidentialEdge` (`edges.py`); `Annotations`
  (`annotations.py`); `BitemporalFields` (`temporal.py`).
- **Relational** (`src/iknos/db/orm.py`): `DocumentContent`, `Action`,
  `DocumentEmbedding`, `PropositionEmbedding`, `PropositionLexicalIndex`.
- Provenance (`db/spans.py::resolve_span_text`, `provenance/action_log.py`),
  bitemporal fields, `override` placeholder, two-annotation rule — **match the
  revised plan, no change needed.**

## Gaps to close

### G0.1 — Tier vocabulary mismatch *(breaking; code-only)*
The revised plan fixes tiers as **`schema → reference → case → working`**
(§9; `architecture.md` §10: `Box.tier ∈ {schema, reference, case, working}`).
Implemented `Tier` is `{AXIOM, DOMAIN, EVIDENCE, DERIVED}`.

- [x] Rename `Tier` members in `types/nodes.py`:
      `AXIOM→SCHEMA`, `DOMAIN→REFERENCE`, `EVIDENCE→CASE`, `DERIVED→WORKING`
      (string values `"schema"/"reference"/"case"/"working"`).
- [x] Update all usages (`Box`, `Fact`, `EvidentialEdge`, tests, any fixtures).
- [x] No data migration required (pre-implementation; AGE stores `tier` as a
      property string and no production graph exists). Document the mapping in the
      `Tier` docstring for anyone with a dev graph.

### G0.2 — New node labels: `Mention`, `Task`
`architecture.md` §10 adds `Mention` (§3.1) and `Task` (§11.2 intentional layer)
to the fixed epistemic schema. Neither is pre-created.

- [x] Add `Mention` and `Task` to `VERTEX_LABELS` (new migration, see *Migration*).
- [x] `Task` is the intentional layer: `type`, `answer_state` (*answered*, not
      adjudicated true/false — keep distinct from epistemic state). Pydantic
      projection lands in Phase 6, but the label + property contract is Phase 0.
- [x] `Mention`: textual mention bound to a canonical entity via `REFERS_TO`
      (§3.1); carries `provisional` + binding confidence. Pydantic projection
      lands with §3.1 extraction (Phase 1, see `gap_phase_1_ingest.md` G1.1).

### G0.3 — New edge labels: identity, part-whole, intentional
Add to `EDGE_LABELS`:

- [x] `REFERS_TO` — `Mention → Actor/Object`. Defeasible, **scored**: carries
      `Annotations` (Layer A/B) + `BitemporalFields`, like `EvidentialEdge`
      (§10). Provisional binding when confidence is low.
- [x] `SAME_AS` — `Actor/Object ↔ Actor/Object` identity assertion (§5.2). Scored,
      candidate/confirmed; the `SAME_AS`-connected component is the canonical
      entity. Same Layer A/B + bitemporal treatment.
- [x] `directPartOf` + `partOf` — the typed, **split** part-whole edges (§14):
      `directPartOf` records each direct decomposition step (intransitive);
      `partOf` is its transitive closure. Both carry a **meronymy-type** tag;
      roll-up is restricted to the component-integral subtype. (Two elabels, per
      `architecture.md` line ~838, rather than one `PART_OF` + property.)
- [x] Intentional-layer edges `DECOMPOSES_INTO` (Task→sub-Task), `ADDRESSES`,
      `RELEVANT_TO` (§11.2).

### G0.4 — `INVOLVES.role` + derived abstraction level (§14)
- [x] Establish the `role` property convention on `INVOLVES` now (string;
      subject/object/instrument…), even though `Actor`/`Object` Pydantic models
      land in Phase 2 — the abstraction-level rule depends on the *subject-role*
      entity. → `Role` StrEnum in `src/iknos/types/edges.py`.
- [x] Record the rule **abstraction level is derived, not stored**: a node's
      level = its subject-role `INVOLVES` entity's position in the `partOf` order
      (§14). No `level` property on reasoning nodes. Optionally materialize a
      depth/rank (recomputed on hierarchy change) for query performance —
      consumer is Phase 6, so a documented placeholder is enough now. → documented
      on `Role` (forward note: depth/rank is a cache of the derived value, never
      an authoritative stored level).

### G0.5 — Intentional layer hooks
- [x] `Hypothesis.acceptability` banding to `true/plausible/implausible/false`
      for presentation (field in the schema contract; banding logic Phase 4/6).
      → `AcceptabilityBand` + pure `band()` (single-source-of-truth thresholds,
      tunable in Phase 4/6) and `HypothesisState` in `src/iknos/types/intentional.py`.
- [x] `Task.answer_state` semantics documented as *answered*, not adjudicated.
      → `AnswerState`/`TaskType` in `intentional.py`; the module docstring draws
      the epistemic-vs-intentional line (Task is *answered*, Hypothesis is
      *adjudicated*). Full `Task`/`Hypothesis` Pydantic projections stay deferred
      (Phase 6 / Phase 4) per the node-projection convention — only the stable
      property vocabularies are fixed now.

### G0.6 — Governance attributes (§9.1)
The schema must carry governance from the start (propagation logic later):

- [x] `sensitivity` on reasoning nodes/edges: lattice label + compartment tags;
      propagated to derived nodes as the **max** of antecedents. Define the
      sensitivity lattice (small ordered set + compartments) in `types/`.
- [x] Source `interest`/role and conditional `credibility` (base reliability ×
      claim-interest alignment) on `Box`/source — **distinct** from faithfulness
      (§3.1) and evidential strength (§8). `Box.reliability_prior` already exists;
      add `credibility`/`interest` modeling alongside it.
- [x] Add `sensitivity` field to `Fact` / `EvidentialEdge`; document the
      max-propagation rule (field now, propagation in the governance track).

### G0.7 — Domain-pack scaffold (§9)
- [x] Define the **epistemic-vs-domain split**: the labels above are the fixed,
      domain-agnostic epistemic schema; the *domain layer* (entity types,
      part-whole taxonomy, rules) is pluggable.
- [x] A **domain pack** = reference/schema-tier `Box`(es) bundling a part-whole
      taxonomy + entity-type ontology + optional rules. Define how a pack is
      declared, versioned, and activated per investigation. New module (e.g.
      `src/iknos/domain/`) + at least one trivial pack loadable end-to-end.
- [x] This is the persistence target for Phase 1 §6.1 "amortize reference
      processing" (`gap_phase_1_ingest.md` G1.8) — packs are ingested once,
      read-only.

### G0.8 — `WITH RECURSIVE` + SCC detection (carried, scope widened)
Still deferred to Phase 3 (no consumer yet), but the revised plan **adds SCC
detection** over `DERIVED_FROM` alongside transitive reachability (well-founded
support + cycle-safe handling, §12).

- [x] Note the SCC requirement on the Phase 3 reachability-helper item so it is
      not lost. No Phase 0 code beyond the note. → already captured in
      `todo_phase_3_reasoning_core.md`: acyclic regions use Counting +
      `WITH RECURSIVE` closure; **cyclic `DERIVED_FROM` SCCs are detected and
      routed to a cycle-safe algorithm (DRed / clingo)**, with must-pass
      ungrounded-vs-grounded-cycle correctness tests. Cross-referenced here so
      the requirement is not lost.

## Migration

One hand-written migration `..._0004_schema_revision.py` (follow the `0001`
conventions — AGE DDL under `ag_catalog`, relational under `public`, no
autogenerate):

- Add vlabels `Mention`, `Task`.
- Add elabels `REFERS_TO`, `SAME_AS`, `directPartOf`, `partOf`,
  `DECOMPOSES_INTO`, `ADDRESSES`, `RELEVANT_TO`.
- No relational DDL (governance/sensitivity are graph-node properties; AGE is
  schema-less for properties). Downgrade drops the new labels.

Tier rename (G0.1) and the new Pydantic fields are **code-only** (no DDL).

## Revised exit criteria (delta over the originals)

- [x] All new labels create-able; the `0004` migration is up/down/up + drift clean
      (the existing `migrations` CI gate). (G0.2–G0.3, #12)
- [x] A `Task` stores with `type`/`answer_state`; a `Mention` binds to an entity
      via a scored `REFERS_TO`. (label + property contract; `0004` smoke test +
      `TaskType`/`AnswerState` vocabulary, G0.2/G0.5)
- [x] A `directPartOf`/`partOf` pair stores with a meronymy-type tag. (G0.7, #14 —
      `tests/integration/test_domain_pack_load.py`)
- [x] A node carries `sensitivity`; a `Box` carries `interest` (credibility is
      derived-not-stored, §9.1/§14) alongside `reliability_prior`. (G0.6, #13)
- [x] A trivial domain pack loads end-to-end (reference-tier box with a tiny
      taxonomy). (G0.7, #14 — `packs/pump_basic.json`)
- [x] `Tier` reads `schema/reference/case/working` everywhere; the Phase 0
      exit-criteria test still passes. (G0.1, #11)
