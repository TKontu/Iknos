# Phase 6 — Investigation Runtime & Analysis

**Goal:** wire the components into the end-to-end investigation loop, run network
analysis on the working subgraph, and present results as claims-with-evidence, not bare
answers.

**Depends on:** Phases 1–5 (ingest, nodes, reasoning core, linking, temporal dynamics).
**Architecture refs:** §11 (investigation loop), §6 (orchestrator/streaming, igraph
analysis), §9 (working set assembly), principle 8.

## Orchestration (§6)

- [ ] Enricher-orchestrator skeleton (flowsint-style): select node → run async operator
      → stream new nodes/edges to the UI.
- [ ] Operators run async (slow LLM/retrieval work); real-time event streaming in the
      `api/` layer.
- [ ] LLM is a swappable component (hosted API for MVP; open-weight self-hosted when
      full self-hosting required).

## The investigation loop (§11), end-to-end

- [ ] **assemble working set:** activate case + reference boxes, create one working
      box, seed edge confidence priors from box `reliability_prior`.
- [ ] **retrieve:** hybrid (dense + sparse), box-scoped.
- [ ] **generate candidates:** the §5.1 funnel (recall-biased; refuters by
      entity/topic).
- [ ] **expand:** run operators on selected nodes and candidate pairs; writes land in
      the working box only; ensemble gate on refutation.
- [ ] **adjudicate:** QBAF state recompute + Counting retraction on the affected
      sub-graph.
- [ ] **analyse:** extract the working subgraph into **igraph/NetworkX** — centrality
      (load-bearing facts), community detection (sub-arguments), hypothesis-support
      pathfinding. (Not in-DB.)
- [ ] **present:** a result = Hypothesis + state + acceptability + its SUPPORTS/REFUTES
      subgraph + resolved provenance (span → text) for every node shown.
- [ ] **revise:** new evidence or expert override (Phase 7) re-enters at expand; only
      the affected sub-graph re-evaluated.

## Presentation (principle 8)

- [ ] Present **ranked probable causes**, each with its evidence subgraph — not a
      single verdict.
- [ ] Surface **unresolved / circular** regions explicitly (oscillation detected in
      adjudication) as a finding, with the subgraph — decide the cyclic-region
      presentation policy (open item, §13).
- [ ] A result is never a bare answer: always claim + evidence subgraph + provenance
      trail.

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
