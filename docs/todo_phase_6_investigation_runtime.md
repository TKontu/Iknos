# Phase 6 — Investigation Runtime & Analysis

**Goal:** wire the components into the end-to-end investigation loop, run network
analysis on the working subgraph, and present results as claims-with-evidence, not bare
answers.

**Depends on:** Phases 1–5 (ingest, nodes, reasoning core, linking, temporal dynamics).
**Architecture refs:** §11 (investigation loop), §6 (orchestrator/streaming, igraph
analysis), §9 (working set assembly), principle 8.

## Entry criteria *(added by the 2026-06-11 review, M3/M4 — design decisions, not code; each is a short decision doc in `docs/`, written in the style of the existing design docs: decisive, one recommendation, alternatives dismissed with reasons)*

- [ ] **`docs/design_api.md` — the API contract.** Enumerate every endpoint Phases
      6–7 need before implementing any: documents/ingest + job status (exists as
      stubs), search/retrieval, investigations (create/activate boxes/Task CRUD +
      decomposition edits), hypotheses (seed/list/state), overrides
      (create/release/reconcile), audit (per-node provenance drill-down, action
      log), review queue (next-N by VoI), graph export. For each: route, verb,
      request/response models (pydantic), and which phase lands it. The event-stream
      transport for operator results (SSE vs WebSocket) is decided here.
- [ ] **`docs/design_authnz.md` — identity before clearance.** §9.1's
      clearance-filtered projection presupposes an authenticated identity carrying
      clearance + compartment claims; nothing establishes one. Decide: OIDC via a
      self-hosted IdP (Authentik/Keycloak — principle 7 compliant), clearance and
      compartments as token claims, FastAPI dependency enforcing them; service
      identities for workers. MVP scope may be "single-operator token auth", but the
      *decision* and the seam must exist before any Phase 7 view ships.
- [ ] **`docs/runbook_deployment.md` — running it for real.** Where the LLM,
      embedding server (R10), MinerU, and the procrastinate worker (R11) run in
      production (extend the Portainer/compose model in
      `project_iknos_deploy`-style docs); the alembic upgrade procedure on deploy;
      domain-pack activation per investigation; and a **restore drill**: actually
      restore a `pg_backup` dump into a scratch instance and verify graph +
      relational + vector data integrity — the backup service exists, restore has
      never been exercised. Record the drill's steps and its last-run date in the
      runbook.

## Orchestration (§6)

- [ ] Enricher-orchestrator skeleton (flowsint-style): select node → run async operator
      → stream new nodes/edges to the UI.
- [ ] Operators run async (slow LLM/retrieval work); real-time event streaming in the
      `api/` layer.
- [ ] LLM is a swappable component (hosted API for MVP; open-weight self-hosted when
      full self-hosting required).

## The investigation loop (§11), end-to-end

- [ ] **frame (§11.2):** set the `Task` (framing question + type); seed hypotheses from
      decomposition + the domain pack's reference hypothesis set + the expert. Scopes
      everything below. (Optional overlay — undirected exploration still possible.)
- [ ] **Completeness guards (§11.2)** so goal-directedness doesn't miss the unframed
      answer: (a) **abductive hypothesis generation** — a high-significance observation
      that no current hypothesis explains spawns a new candidate hypothesis/sub-Task; (b)
      a **residual exploration budget** that never falls to zero (some unscoped retrieval/
      candidate-gen always runs); (c) a periodic **undirected sweep** as a completeness
      check. An unexplained high-significance observation is itself a finding.
- [ ] **assemble working set:** activate case + reference boxes (incl. the Task's domain
      pack), create one working box, seed edge confidence priors from box
      `reliability_prior`.
- [ ] **retrieve:** hybrid (dense + sparse), box- and Task-scoped.
- [ ] **generate candidates:** the §5.1 funnel (recall-biased; refuters by
      entity/topic), prioritized toward the Task's hypotheses.
- [ ] **expand:** run operators on selected nodes and candidate pairs; writes land in
      the working box only; ensemble gate on refutation. **Corroboration counts only
      across independent sources** (sources tracing to one origin are one — reuses
      provenance/§5.2), so copy-flooding doesn't manufacture support (§9.1).
- [ ] **adjudicate:** QBAF state recompute + well-founded retraction on the affected
      sub-graph (Counting/DRed, §12).
- [ ] **analyse:** extract the working subgraph into **igraph/NetworkX** — centrality
      (load-bearing facts), community detection (sub-arguments), hypothesis-support
      pathfinding. (Not in-DB.)
- [ ] **triage (§11.1):** rank all needs-human items by value of information
      (`leverage × uncertainty × significance`) into one budgeted top-N queue. Leverage
      from centrality + Layer A support-counts + QBAF perturbation (decision-relevance);
      **two-tier cost:** cheap structural proxies rank the whole queue, QBAF perturbation
      runs only on the top-k (it re-solves per item — not free at scale).
      uncertainty from the aggregated confidence types (and *which* type → what judgment
      is needed); include fragile-/conflicting-confidence items, not only uncertain ones.
      No LLM in the ranking; re-rank between batches, not per click; graceful fallback to
      significance + recency. The same VoI score gates the **re-inference budget**
      (§6.1): expensive LLM re-analysis on a changed region runs only where VoI is above
      threshold — cheap symbolic re-propagation runs everywhere, LLM re-inference only
      where it could change the conclusion.
- [ ] **sufficiency (§11.2):** stop expanding when the Task is answered to threshold or
      the VoI of further work drops below threshold — the principled stopping point.
- [ ] **present:** the answer to the Task — its addressing Hypotheses + state +
      acceptability (banded true/plausible/implausible/false) + SUPPORTS/REFUTES subgraph
      + resolved provenance (span → text) for every node shown.
- [ ] **revise:** new evidence or expert override (Phase 7) re-enters at expand; only
      the affected sub-graph re-evaluated. Entity resolution runs **continuously** here —
      accumulating facts can confirm a candidate merge or trigger a split (§5.2), which
      re-evaluates the affected component.

## Presentation (principle 8)

- [ ] Present **ranked probable causes**, each with its evidence subgraph — not a
      single verdict.
- [ ] Surface **unresolved / circular** regions explicitly (oscillation detected in
      adjudication) as a finding, with the subgraph — decide the cyclic-region
      presentation policy (open item, §13).
- [ ] A result is never a bare answer: always claim + evidence subgraph + provenance
      trail.

## Abstraction-level presentation (§14)

- [ ] Audience views as **projections** (a cut through the `PART_OF` DAG), not new
      data: management rolls up to coarse ancestor entities with summaries; experts
      drill to leaves.
- [ ] **Mixed-level frontier:** build on the established *degree-of-interest*
      framework (Furnas; van Ham & Perer search/show-context/expand-on-demand) but
      replace "a-priori importance − distance" with **"evidence significance −
      distance"** — descend a subtree only where finer facts are load-bearing (high
      evidence weight / contested / where hypotheses sit), weighted by the §6 signals.
      Note: a single global resolution parameter provably cannot do this (resolution
      limit) — selection is per-region. (Significance-weighting is novel; open item,
      §13/§14.)
- [ ] Level derived from the subject entity's `partOf` position; surface ambiguous
      attachment rather than forcing a single level.

## Exit criteria

- [ ] A full investigation runs: assemble → … → present, on the fixture/gate corpus.
- [ ] Network analysis returns centrality/community/path results on the working
      subgraph.
- [ ] Results show ranked causes with evidence and provenance; unresolved regions are
      visible, not hidden.

## Phase risks / decisions

- Working-set size must stay in-memory for subgraph extraction (§6 load-bearing
  assumption) — watch graph size; in-DB analytics only if it ever breaks.
- Cyclic-region presentation policy is undecided — settle here (§13).
