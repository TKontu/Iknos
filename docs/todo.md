# Iknos — Development Plan

Top-level roadmap. Each phase has a dedicated `todo_phase_N_*.md` with detailed
tasks, dependencies, and exit criteria. **`architecture.md` is the source of truth**
for every design decision; phase files reference it by section (§).

## Build philosophy

- **Thin slice first, then harden.** Build minimal versions through Phases 1–4,
  prove the end-to-end loop with the validation experiment, *then* scale and harden.
  Do not gold-plate a layer before the loop works.
- **Provenance and audit are not a phase — they are present from day one.** Every
  node, edge, and operator carries provenance and emits a process-action record from
  Phase 0 onward (principles 4 and 9). Auditability cannot be retrofitted.
- **Two annotations from day one.** Every fact/edge carries both an integer
  support-count (Layer A) and a `[0,1]` confidence (Layer B); never collapsed (§12).
- **LLM proposes, engine disposes (principle 6).** No LLM output mutates a maintained
  value directly; it enters as a defeasible, provenance-stamped input.
- **Build, not buy; self-hosted, open-source (principle 7).** No commercial
  components; copyleft only as reference to re-implement.
- **Present the network, not a verdict (principle 8).** The system never has to
  converge; unresolved/circular structure is surfaced, not forced.

## Phases

| # | Phase | Outcome |
|---|-------|---------|
| 0 | Foundations & data model | Storage engine, schema contract, provenance + audit plumbing |
| 1 | Ingest pipeline | Text → spans → propositions → indexed |
| 2 | Graph construction | Boxes/tiers, node extraction, provenance edges, audit logging |
| 3 | Reasoning core | Two-layer propagation (truth maintenance + confidence) + derivation |
| 4 | Evidence linking & adjudication | Candidate generation, edge judgment, QBAF, hypothesis state → **validation gate** |
| 5 | Temporal dynamics & belief revision | Bitemporal supersession, revision triggers, box deprecation |
| 6 | Investigation runtime & analysis | The investigation loop end-to-end, network analysis, presentation |
| 7 | Expert interface | Graph view, audit drill-down, soft override & reconciliation |

## Dependency order

```
Phase 0  Foundations
   │
   ├──────────────► Phase 1  Ingest
   │                   │
   │                   ▼
   │                Phase 2  Graph construction (nodes)
   │                   │
   │      ┌────────────┴────────────┐
   ▼      ▼                         ▼
Phase 3  Reasoning core      Phase 4  Evidence linking & adjudication
(truth maint. + confidence)  (candidate gen, edges, QBAF, hypothesis state)
   └────────────┬────────────┘
                ▼
        ╔═══════════════════╗
        ║ VALIDATION GATE   ║  §8 experiment — thin slice, planted corpus
        ╚═══════════════════╝
                │
                ▼
        Phase 5  Temporal dynamics & belief revision
                │
                ▼
        Phase 6  Investigation runtime & analysis
                │
                ▼
        Phase 7  Expert interface
```

Phases 3 and 4 are built as **thin implementations in parallel after Phase 2**, then
validated together at the gate before either is hardened. Phases 5–7 assume a
validated core.

## Validation gate (between Phase 4 and Phase 5)

The architecture's first milestone (§8 *Proposed small-scale experiment*). On a small
fixed corpus with deliberately planted contradictions and a later overturning fact,
measure:

- [ ] Counting-based retraction propagates correctly and stays local (Phase 3)
- [ ] QBAF hypothesis state flips correctly when the overturning fact lands (Phase 4)
- [ ] Consistency-based confidence beats raw verbalized confidence on the planted set
- [ ] Ensemble contradiction detection beats a single LLM call
- [ ] Candidate generation recalls the planted edges — **especially the refuting ones**
      (the dissimilar-refuter test, §5.1)

**Do not harden any layer until the gate passes.** A failure here changes the design,
not just the code.

## Cross-cutting tracks (run through every phase)

- **Auditability & provenance** — every artifact traceable to source spans and the
  action that produced it (§10.1, §10.2).
- **Testing** — unit per component; an end-to-end fixture corpus maintained from
  Phase 1; the planted-contradiction corpus from the gate kept as a regression suite.
- **Licensing/compliance** — track dependency licenses; keep the fully-open stack
  (Postgres + AGE + pgvector, igraph, clingo) viable; isolate any GPL reference code.

## Open questions & risks

Tracked in `architecture.md` Open items and §13. The live, build-time/empirical ones
are surfaced in the phase that must resolve them:

- Re-evaluation trigger policy (eager vs lazy) → Phase 5
- LLM→QBAF weight mapping → Phase 4 + gate
- Candidate-generation recall tuning, dissimilar-refuter recall → Phase 4 + gate
- Cyclic-region presentation policy → Phase 6
- Truth-maintenance placement (in-Postgres vs alongside via DBSP) → Phase 3 (MVP), revisit at scale
- LLM judging bias / correlated error → Phase 4 (disciplines) + Phase 7 (expert calibration loop)
