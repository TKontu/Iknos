# Iknos — Development Plan

Top-level roadmap. Each phase has a dedicated `todo_phase_N_*.md` with detailed
tasks, dependencies, and exit criteria. **`architecture.md` is the source of truth**
for every design decision; phase files reference it by section (§).

## Build philosophy

- **Thin slice first, then harden.** Build minimal versions through Phases 1–4,
  prove the end-to-end loop with the validation experiment, *then* scale and harden.
  Do not gold-plate a layer before the loop works.
- **Earn the complexity; prove efficacy on real evidence.** The synthetic gate proves
  mechanisms, not worth — so beat a cheap baseline (RAG / agentic RAG / expert+search) on
  the differentiator axes *early* as a go/no-go, ablate to find the value-carrying
  components, and climb the validity ladder (synthetic → retrospective real →
  prospective). Never claim efficacy from the synthetic gate alone (Instrument E).
- **Provenance and audit are not a phase — they are present from day one.** Every
  node, edge, and operator carries provenance and emits a process-action record from
  Phase 0 onward (principles 4 and 9). Auditability cannot be retrofitted.
- **Two annotations from day one.** Every fact/edge carries both an integer
  support-count (Layer A) and a `[0,1]` confidence (Layer B); never collapsed (§12).
- **LLM proposes, engine disposes (principle 6) — including at the perception layer.**
  No LLM output mutates a maintained value directly; it enters as a defeasible,
  provenance-stamped input. This extends to *extraction*: propositions carry structured
  epistemic fields (polarity/modality/attribution), references are bound by scored
  `REFERS_TO` edges, extraction is verified (extract-then-verify), and low-faithfulness
  atoms are quarantined from high-stakes use and routed to expert review (§3.1).
- **Build, not buy; self-hosted, open-source (principle 7).** No commercial
  components; copyleft only as reference to re-implement.
- **Present the network, not a verdict (principle 8).** The system never has to
  converge; unresolved/circular structure is surfaced, not forced.
- **Four distinct hierarchies, never conflated (§2, `DERIVED_FROM`, §14, §6).** Text
  chunk levels, the derivation structure, the entity part-whole (`PART_OF`) partonomy,
  and the associative community structure (§6) are separate; community ≠ partonomy. A
  fact's abstraction level is *derived* from its referent's position in `partOf`, not
  stored; views are projections (cuts) over it.
- **Goal-directed by a first-class Task (§11.2).** An investigation is framed by a
  `Task` (the question + type) in a distinct *intentional* layer — answered, not
  adjudicated. It scopes retrieval, candidates, and hypothesis formation, defines the
  decision VoI/re-inference optimize for, and supplies the stopping criterion (answered
  to threshold). Hypotheses seed from decomposition + domain-pack reference sets + the
  expert; true/plausible/implausible/false is banded acceptability. Optional overlay —
  undirected exploration still works.
- **Bounded, incremental cost (§6.1).** Reference corpus processed once and reused;
  LLM outputs content-addressed-cached; symbolic re-propagation on the delta runs freely
  (no LLM); expensive LLM re-inference is VoI-gated and budget-bounded. Changes touch
  only related parts — linear in the affected region, never exponential.
- **Direct scarce expert attention by value of information (§11.1).** The system
  produces more than any expert can review, so one ranked, budgeted queue — fed by every
  needs-human signal and ordered by `leverage × uncertainty × significance` — decides
  what gets reviewed first. No LLM in the ranking; it must beat cheaper baselines or be
  replaced by them.
- **Multi-domain via pluggable domain packs (§9).** The epistemic schema (facts,
  conclusions, hypotheses, evidence) is fixed and domain-agnostic; the domain layer
  (entity types, part-whole taxonomy, rules) is a domain pack = reference-tier box(es).
  Reliability is a function of anchoring: a domain works well to the extent its pack's
  taxonomy covers the referents; thin coverage means provisional levels + review.

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
| + | Presentation views *(optional)* | Radar, **table/bulk-edit** & coordinated projections of the hypothesis layer — `todo_presentation_views.md`. Editing via existing override machinery; no backend changes; layered on Phase 7 |

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

- [ ] Counting/DRed well-founded retraction propagates correctly and stays local (Phase 3)
- [ ] QBAF hypothesis state flips correctly when the overturning fact lands (Phase 4)
- [ ] Consistency-based confidence beats raw verbalized confidence on the planted set
- [ ] Ensemble contradiction detection beats a single LLM call
- [ ] Candidate generation recalls the planted edges — **especially the refuting ones**
      (the dissimilar-refuter test, §5.1)
- [ ] Extraction faithfulness and entity resolution clear their bars (Trials A5, A6)

**This synthetic gate proves *mechanisms*, not *efficacy*.** Two more checks are required
before committing to the full build:

- [ ] **Beat the cheap baseline (go/no-go, Trial E1)** — material lift over plain RAG /
      agentic RAG / expert+search on the differentiator axes (contradiction handling,
      retraction, traceability, calibration), bias-controlled. If not, **stop and rethink**.
- [ ] **Ablation (Trial E2)** — which components carry the value; the data-driven minimal
      system if descoping is needed.
- [ ] **Ecological validity (Trial E3)** — run on a real, already-resolved case (messy
      evidence, known answer). Climb the ladder: synthetic → retrospective real →
      prospective/live; never claim efficacy from the synthetic gate alone.

**Do not harden any layer or start Phases 5–7 until the gate passes *and* E1 is go.** A
failure here changes the design, not just the code.

## Cross-cutting tracks (run through every phase)

- **Auditability & provenance** — every artifact traceable to source spans and the
  action that produced it (§10.1, §10.2).
- **Testing** — unit per component; an end-to-end fixture corpus maintained from
  Phase 1; the planted-contradiction corpus from the gate kept as a regression suite.
- **Licensing/compliance** — track dependency licenses; keep the fully-open stack
  (Postgres + AGE + pgvector, igraph, clingo) viable; isolate any GPL reference code.
  **MinerU (parse front-end) is AGPL-3.0 — invoke it as a separate hosted service
  (CLI/HTTP), never vendor/link it into the codebase**, so the copyleft stops at the
  service edge (§1).
- **Governance (§9.1)** — sensitivity labels propagate over provenance and gate views by
  clearance; source credibility is conditional (base × interest, against-interest boost)
  and track-record-revised; corroboration is independence-aware; packs are versioned/
  bitemporal; cold-start runs induce-mode + promotion-bootstrap. Present from the schema
  up, not bolted on.
- **Operations & security** *(added by the 2026-06 review — previously absent from every
  phase)* — the §9.1 clearance projection presupposes **authentication/authorization**
  that no phase builds: scope authn/z with the API (Phase 6) so clearance filtering has
  an identity to filter on. Plus: containerized packaging incl. the MinerU AGPL
  service-edge enforced in build tooling (not just prose); Postgres **backup/restore**
  for what is the durable record of investigations; basic observability (structured
  logs, LLM-call metrics, queue depth). Concrete deliverables land in Phases 6–7; the
  track exists so they are scoped, not discovered.

## Open questions & risks

Tracked in `architecture.md` Open items and §13. The live, build-time/empirical ones
are surfaced in the phase that must resolve them. The **2026-06 review**
(`review_2026-06_architecture_plan.md`) is folded into the plan as: G1.13–G1.19
(`gap_phase_1_ingest.md`), G0.R2 (`gap_phase_0_residual.md`), Phase 2 entry criteria,
the Phase 3 semiring decision, and the A0/C3/E1 scheduling deltas (`todo_trials.md`):

- Re-evaluation trigger policy (eager vs lazy) → Phase 5
- LLM→QBAF weight mapping → Phase 4 + gate
- Candidate-generation recall tuning, dissimilar-refuter recall → Phase 4 + gate
- Cyclic-region presentation policy → Phase 6
- Truth-maintenance placement (in-Postgres vs alongside via DBSP) → Phase 3 (MVP), revisit at scale
- Layer B semiring: Viterbi depth-bias vs Gödel depth-neutral → Phase 3 entry (fixture-decided, §12)
- Long-document windowed embedding (no silent truncation) → Phase 1 (G1.13)
- AGE viability under schema density + property indexes → Phase 2 entry (C3 + G0.R2)
- LLM judging bias / correlated error → Phase 4 (disciplines) + Phase 7 (expert calibration loop)
- Part-whole hierarchy acquisition quality (taxonomy anchor / meronymy / relative ordering) → Phase 2 + gate
- Mixed-level frontier rendering (adaptive abstraction per audience/region) → Phase 6
