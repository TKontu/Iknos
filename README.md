# Iknos

**A self-hosted system that turns text from any domain into a traceable, non-monotonic reasoning graph — so experts can investigate complex problems, at the right level of detail, and see *why* the system believes what it does.**

Iknos ingests arbitrary documents, extracts facts, derives conclusions and hypotheses, and links them with evidential edges (*supports* / *refutes*). Facts attach at their place in a domain's part-whole hierarchy, so the same network can be read at a management level or drilled to expert detail. The result is a knowledge network an expert can explore, audit back to source text, and correct — built for research, investigations, and root-cause analysis, across different domains.

> **Status: pre-implementation.** This repository currently holds the architecture and design. The MVP is not yet built. See [`docs/architecture.md`](docs/architecture.md) for the full specification and [Roadmap](#roadmap) for the staged build. Install and usage instructions will follow the first working stage.

---

## What it is for

Some problems aren't answered by retrieval — they're answered by *reasoning over evidence that conflicts, accumulates, and gets overturned*. A failure investigation, a literature synthesis, a root-cause analysis: the work is assembling facts from many sources, drawing conclusions, forming competing hypotheses, and tracking which evidence supports or refutes each one as new information arrives.

Iknos is built for that. It is an **expert tool**: its output is an *initial conclusion* — a starting point for expert review — not a verdict to be trusted blindly.

## What makes it different

- **Non-monotonic.** New evidence can *withdraw* support and overturn earlier conclusions. The graph is built to retract, not only to grow.
- **Present the network, not a verdict.** The system surfaces ranked probable causes, each with its evidence subgraph, in traceable form. It is not required to converge on a single answer — unresolved or circular evidence is a *finding*, not a defect.
- **Auditable by construction.** Every node and edge traces back to the source spans and the operator decisions that produced it. Nothing is hard-deleted; history is preserved bitemporally.
- **Expert-in-the-loop.** Any computed value, content, or edge can be **soft-overridden** from the graph view — the original is retained, the override is logged, reversible, and feeds back as a calibration signal.
- **LLM proposes, the engine disposes.** The LLM never mutates a maintained conclusion directly; every LLM output is a defeasible, provenance-stamped input. Consistency, scoring, and retraction are decided by the symbolic layer.
- **Multi-level by construction.** Facts attach at their referent's position in a domain part-whole hierarchy (gearbox ⊃ shaft ⊃ bearing ⊃ roller). The network can be presented at the level relevant to the audience — abstract for management, fine-grained for domain experts — and, because the most significant knowledge does not sit at one uniform level, different regions can be shown at different resolutions at once.
- **Multi-domain via pluggable domain packs.** The reasoning schema (facts, conclusions, hypotheses, evidence) is fixed and domain-agnostic; the domain layer — entity types, the part-whole taxonomy, domain rules — is plugged in as a *domain pack* (e.g. an ISO 14224 equipment taxonomy, a bill of materials, an anatomy ontology). The system is reliable in a domain to the extent that domain's authoritative structure can be plugged in.
- **Build, not buy; self-hosted, open-source.** No commercial components. Where a fork is needed, permissive licenses are preferred and copyleft dependencies are treated as reference implementations.

## How it works

Two distinct flows.

**Ingest — how knowledge gets in** (semantic-aware chunking → a reasoning graph):

```
text → embedding substrate → segmentation backbone → proposition layer → indexing → reasoning graph
```

Text is embedded once and chunked at multiple abstraction levels; sub-paragraph spans are decontextualized into atomic, self-contained **propositions**; facts, actors, objects, conclusions, and hypotheses are extracted as typed nodes; everything is indexed (dense + sparse) with source references retained.

**Investigation — how an analysis runs** over the graph:

```
assemble working set → retrieve → generate candidates → expand (operators) → adjudicate → analyse → present → revise
```

An investigation activates the knowledge *boxes* it needs — including the relevant **domain pack(s)** — retrieves relevant evidence, generates candidate node pairs cheaply (so the expensive step is targeted, not all-pairs), runs reasoning **operators** (`extract`, `deduce`, `induce`, `corroborate`, `find-contradiction`), adjudicates hypothesis state, and presents each probable cause with its evidence subgraph and provenance — rendered at the audience's abstraction level via a mixed-level cut through the part-whole hierarchy. New evidence or an expert override re-enters the loop and only the affected sub-graph is re-evaluated.

A result is never a bare answer: it is always the claim **plus its evidence subgraph plus the provenance trail**.

## Architecture at a glance

- **Storage — one engine.** PostgreSQL + **Apache AGE** (property graph) + **pgvector** (embeddings). Graph, text, offsets, and vectors co-located, so provenance resolution is a local join.
- **Reasoning graph.** Typed nodes (facts, actors, objects, deductive/inductive conclusions, hypotheses) and evidential edges. Each evidential edge separates **sign** (supports/refutes), **strength** (how strongly it bears), and **significance** (how much it matters if true).
- **Knowledge organization.** Two axes: a **tier** (schema → reference → case → working) that drives reasoning and entrenchment, and a **box** (lifecycle/provenance unit). A *domain pack* is a set of reference-tier boxes carrying a domain's taxonomy, entity types, and rules — this is how new domains are added.
- **Abstraction.** A `PART_OF` hierarchy over entities (typed; only the component-integral subtype rolls up transitively). A fact's level is *derived* from its referent's position, anchored to the domain taxonomy where possible and induced from text only as a fallback. Audience views are projections; the significance-weighted **mixed-level frontier** is a deliberately novel piece.
- **Propagation — two layers, by necessity.** A commutative-group **truth-maintenance** layer (derivation counts; Counting/DRed, or Differential Dataflow / DBSP at scale) owns retraction; an absorptive-semiring **confidence** layer (Viterbi `max-·`, or Gödel `max-min`) owns strength. They are separate because clean deletion needs an additive inverse and confidence aggregation needs idempotence — one structure cannot have both.
- **Adjudication.** Supports/refutes are adjudicated by a **Quantitative Bipolar Argumentation Framework** with gradual semantics; confidence feeds it as base scores.
- **Network analysis.** Per-investigation subgraphs are extracted into **igraph / NetworkX** (centrality, community detection, pathfinding) rather than analysed in the database.
- **Symbolic checks.** **clingo** (ASP) for consistency constraints and defeasible rules.
- **Orchestration.** A flowsint-style operator/orchestrator skeleton with real-time streaming; the LLM is a swappable component.

The reasoning, confidence, and bias-handling layers above the database are bespoke — no off-the-shelf system packages this combination. That integration is the substance of the project.

## Planned repository layout

Mirrors the module split in the architecture (subject to change before first commit):

- `types/` — the shared data model (the schema contract)
- `core/` — orchestrator, truth-maintenance + confidence propagation, belief revision
- `operators/` — the reasoning operators (extract, deduce, induce, corroborate, find-contradiction)
- `api/` — service layer with real-time event streaming
- `app/` — the graph analysis view (node expansion, audit, expert override)

## Roadmap

The staged build (details in [`docs/architecture.md`](docs/architecture.md) §8):

0. **Invariant** — symbolic state authoritative; LLM proposes, engine disposes.
1. **Retraction core** — well-founded-support truth maintenance; a conclusion survives iff it re-grounds in base facts (so unfounded cycles drop).
2. **Confidence valuation** — Viterbi/Gödel least-fixpoint over the supported sub-graph.
3. **Gradual adjudication** — QBAF hypothesis state from confidence-weighted supports/refutes.
4. **Confidence fusion** — calibrated, source-discounted edge strengths from multi-sample LLM judgments.
5. **Bitemporality** — event + ingestion time, validity windows, non-lossy supersession.
6. **Ensemble contradiction** — multi-sample LLM + symbolic + temporal agreement before any refutation.

The first milestone is a **small-scale experiment** on a fixed corpus with planted contradictions and an overturning fact, to measure retraction propagation, hypothesis-state flips, confidence calibration, contradiction detection, and candidate recall before any layer is hardened.

## Open questions

Tracked in [`docs/architecture.md`](docs/architecture.md) (Open items). The live ones are empirical or build-time, not design gaps: the re-evaluation trigger policy, the LLM→argument-weight mapping, candidate-generation recall tuning, the mixed-level-frontier rendering policy, part-whole acquisition quality, domain-pack coverage and portability across domains, cyclic-region presentation, and where the truth-maintenance layer physically lives (in-Postgres vs alongside). Known risks — LLM judging bias and correlated error, evaluation bias, cyclic convergence, and the bespoke integration seams (the significance-weighted frontier and the fact→referent level operator are genuinely novel) — are documented in [`docs/architecture.md`](docs/architecture.md) §13.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — the authoritative design: principles, pipeline, schema contract, runtime loop, propagation model, auditability and expert-override mechanics, and the risk register. **Read this first** to contribute or extend; it is the source of truth and the README defers to it.
- [`docs/todo.md`](docs/todo.md) — the staged development plan; each phase has a dedicated `docs/todo_phase_N_*.md` with tasks, dependencies, and exit criteria.
- [`docs/todo_trials.md`](docs/todo_trials.md) — the trial & experiment plan (pre-implementation gates).
- [`docs/iknos-pipeline-reference.html`](docs/iknos-pipeline-reference.html) — a visual processing & data-flow reference.
- **Gap plans** ([`docs/gap_phase_0_foundations.md`](docs/gap_phase_0_foundations.md), [`docs/gap_phase_1_ingest.md`](docs/gap_phase_1_ingest.md)) — code revisions to bring the already-implemented Phase 0/1 code in line with this revised plan.

## License

Intended to be open-source and self-hostable. License to be finalized; the project avoids commercial dependencies and prefers permissive licenses, treating any copyleft (GPL) reference implementations as designs to re-implement rather than ship.
