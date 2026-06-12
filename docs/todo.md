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
      system if descoping is needed. *(The W10 de-scoping ladder in `todo_trials.md`
      names the candidate minimal configurations in advance, so a mixed E1 result has
      a pre-agreed landing zone.)*
- [ ] **Ecological validity (Trial E3)** — run on a real, already-resolved case (messy
      evidence, known answer). Climb the ladder: synthetic → retrospective real →
      prospective/live; never claim efficacy from the synthetic gate alone.

**Do not harden any layer or start Phases 5–7 until the gate passes *and* E1 is go.** A
failure here changes the design, not just the code.

**Gate-asset status (2026-06-11 review, F1): the gate is now the critical path and
none of its assets exist.** Phase 4's core shipped (G4.1–G4.5 slice 1); the next
work is **not** further feature slices but the gate assets themselves: the planted
corpus (V1), gold labels + second annotator (V2 — the longest-lead item in the
project), the metrics harness (V3), and the E1 baseline rigs (V4–V6) — specs in
`todo_trials.md`. The safety lockdown R8→R9→V7→V8 (quarantine + ensemble filter)
must land before the remaining G4.5 slices — **complete: R8/R9/V7/V8 all
shipped** — specs in
`todo_phase_4_linking_adjudication.md` *Open task specs*. Gate infrastructure
(R10/R11 out-of-process embeddings + job queue; R4/V9 ANN) lands before the trials
run — **R4 (HNSW indexes) and V9 (k-NN push-down + recall measurement) shipped;
R10/R11 next.** A 2026-06-12 residual review of the landed gate assets + ingest
batch (record: `archive/review_2026-06-12_completed_scope_residuals.md`) found no
blockers but folded four fix tasks into the plan: **V12** (baseline-rig hardening —
land before any E1 measurement run) and **V13** (gate-corpus touch-ups — land
**before V2 labeling starts**, after which the corpus freezes) in `todo_trials.md`;
**V14** (V9 push-down verification hardening — before the G4.6 flip decision) in
`todo_phase_4_*.md`; **G1.25** (G1.22 backfill-skip fix + docstring sync) in
`todo_phase_1_ingest.md`.

**The composed loop now runs as a system (2026-06-11 architecture assessment,
W1/W2/W3 — all shipped).** The `REFUTES → retract → A → B → QBAF → gate` feedback
path is owned by `core/revision_loop.py` (**W1**), driven by the G3.9 `stabilize`
driver; the synthetic §8 end-to-end fixture (**W2**, `test_revision_loop_e2e.py`)
proves the mechanics on real AGE with zero LLM calls; and the interim refutation-gate
choice (**W3**) was decided eyes-open as **option (a): ship the minimal clingo
symbolic-channel producer** (`core/symbolic_gate.py`), so `DEFAULT_GATE`'s required
SYMBOLIC channel is now wired and an automated `refuted` flip is reachable on genuine
LLM + symbolic agreement (no longer non-functional). Specs in `todo_phase_4_*.md`
*Open task specs*.
The assessment also gates Phase 5 entry (see `todo_phase_5_*.md`) and adds
G1.23/G1.24, W7/W8/W11, the C3 W9 amendment, and the E2 de-scoping ladder (W10).
Findings record: `archive/review_2026-06-11_planned_architecture_assessment.md`.

## Deferred items — triggers (check on every slice PR)

*(Added by the 2026-06-11 review, F3.)* A deferral recorded as prose with an
implicit trigger gets missed when the triggering slice ships — it happened twice
(quarantine enforcement and A0 scheduling). This table is the single place deferred
items wait. **Rule: every slice PR checks this table and states in the PR body
which triggers (if any) its change fires.** When a trigger fires, move the item
into the owning phase/gap doc as an active task.

| Deferred item | Recorded in | Trigger — re-open when… | Status |
|---|---|---|---|
| Quarantine enforcement (G1.6/R9) | `todo_phase_4_*.md` *Open task specs* | a `REFUTES` creation site exists | **CLOSED** — R8→R9→V7 all shipped (V7 #77; edge producer enforces §3.1) |
| Ensemble-gate SYMBOLIC channel producer (W3) | `todo_phase_4_*.md` *Open task specs* (W3), `core/ensemble_gate.py` docstring | the differentiator must auto-refute (gate-trial window / G4.6) | **CLOSED** — W3 chose option (a): clingo producer shipped (`core/symbolic_gate.py`) |
| Ensemble-gate TEMPORAL channel producer (§7.4) | `core/ensemble_gate.py` docstring (`temporal_channel`) | a time-sensitive sub-domain needs the bitemporal-overlap veto, or `STRICT_GATE` is adopted | waiting (later G4.5 / Phase 5 bitemporal) |
| Ensemble-gate-only `refuted` flip (§7.2) | `todo_phase_4_*.md` *Open task specs* (V8) | any consumer writes `Hypothesis.state` | **FIRED** (G4.4 `persist_verdicts`) → **V8 shipped** |
| pgvector ANN index + k-NN push-down | `todo_phase_4_*.md` *Open task specs* (R4/V9), `core/candidates.py` docstring | k-NN runs beyond working-set scale, or the gate measures recall | **CLOSED** — R4 + V9 shipped; V14 (verification hardening) open before the G4.6 flip |
| Out-of-process embeddings + job queue (R10/R11) | `todo_trials.md` *Gate prerequisites* | first real multi-document corpus ingest | **FIRED** (V1 corpus is that ingest) → land before gate trials |
| G1.10 Part B — RAPTOR summary levels | `todo_phase_1_ingest.md` *Open work* | coarse-to-fine candidate stage (§5.1 stage 3) or retrieval needs coarse levels | waiting |
| G1.11 — box scoping on dense/sparse indexes | `todo_phase_1_ingest.md` *Open work* | first hybrid-retrieval consumer (Phase 6 `retrieve`) | waiting |
| G1.19 — RRF rank fusion | `todo_phase_1_ingest.md` *Open work* | same trigger as G1.11 | waiting |
| G1.12 — multi-span provenance | `todo_phase_1_ingest.md` *Open work* | a consumer needs >1 span per proposition | waiting (optional) |
| G1.7 cascade re-extraction on stale spans | `todo_phase_1_ingest.md` *Open work* | a model/prompt change must purge-and-recreate in production | **CLOSED** — shipped as G1.7r (#68) |
| G3.3 — clingo foundedness for recursion/negation | `todo_phase_3_reasoning_core.md` *Build record* | first negation/recursive rule producer (domain-pack rules) | waiting |
| Bitemporal as-of range indexes | migration `0007` docstring | Phase 5 supersession reader defines the as-of query shape | waiting |
| Per-model recalibration curve (identity until calibrated) | `todo_phase_4_*.md` status block | G4.6 produces calibration data | waiting (G4.6) |
| `SignificancePolicy.tier_weight` (uniform 1.0) | `todo_phase_4_*.md` status block | G4.6 produces calibration data | waiting (G4.6) |
| Pronoun/local-discourse binding stage (§3.1 cascade) | `todo_phase_2_*.md` *Deferred seams* (G2.4) | A5 measures binding accuracy and finds it binding | waiting |
| Phase 2 deferred seams (per-increment list) | `todo_phase_2_*.md` *Deferred seams* | each names its owning phase — transplant when that phase starts | waiting |
| Edge-property GIN indexes | migration `0007` docstring | a box-scoped *edge*-property query path exists | waiting |

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
are surfaced in the phase that must resolve them. All review findings (June 2026,
the 2026-06-11 post-Phase-4 review, the 2026-06-11 architecture assessment
W1–W11, and the 2026-06-12 completed-scope residual review G1.25/V12–V14) are
**fully folded into the plan**: shipped fixes are recorded in each
phase file's *Build record*; open tasks live as specs in `todo_phase_4_*.md` (*Open
task specs*), `todo_phase_1_ingest.md` (G1.23/G1.24), `todo_trials.md` (A0/E1 work
breakdowns + *Gate prerequisites* + the C3/E2 amendments), the Phase 5/6/7 entry
criteria, the deferred-triggers table above, and the *Maintenance
backlog* below. The original review/gap documents are preserved verbatim in
`docs/archive/` (historical record only — not task trackers; code docstrings that
cite a gap file by name resolve there):

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

## Maintenance backlog *(opportunistic — no phase gate; merged from R12/V11)*

- [ ] **R12 — Action metrics (observability floor).** Migration: `ALTER TABLE
      actions ADD COLUMN metrics JSONB NOT NULL DEFAULT '{}'::jsonb`; `record_action`
      accepts optional `metrics`. Populate from the instrumented paths:
      `core/llm.py` returns usage (prompt/completion tokens) alongside the parsed
      result; extract/verify Actions get `{duration_ms, prompt_tokens,
      completion_tokens, n_samples, cache_hit}`; parse/segment Actions get
      `{duration_ms, n_spans, n_skipped_whitespace}`. `time.monotonic()` deltas;
      absent usage → keys omitted, never zeroed. Cost discipline (§6.1) and Trials
      A/C consume these numbers.
- [~] **V11 — unit tests for untested infrastructure modules** *(in progress —
      domain-loader result-builder tests #64, `_build_cypher_sql` seam + assembly
      tests #65, `GRAPH_NAME` identifier validation + tests #66 shipped)*. Check
      existing coverage first (`test_age_cypher_map.py` covers `cypher_map` — don't
      duplicate). Remaining: `test_action_log.py` (record construction: required
      fields, JSON-serializable payloads — extract a pure `build_action` seam if
      the module is all-DB, behavior-identical); `test_audit.py` (pure parts of
      reach-back assembly); `test_boxes_registry.py` (serde round-trip);
      `test_config.py` (defaults without env; env overrides; no DB on import).
      Pure where the module is pure; integration tests keep owning round-trips;
      don't chase coverage into ORM/type modules.
- [x] **W7 — dual-write transaction discipline** *(2026-06-11 architecture assessment, P7 —
      Phase-5 entry criterion)* *(shipped)*. `db/age.py::atomic_write(session)` is the wrapper —
      an `@asynccontextmanager` that commits the bracketed writes together on clean exit and
      **rolls the whole unit back on any exception** before re-raising (no orphaned `Action`, no
      orphaned vertex, no half-written edge set). It lives beside the graph-write helpers operators
      already import and stays DB-free on import (no engine), unlike `db/session.py`. Adopted in the
      §6 graph operators that commit: `derive._propose`, `extract._persist`, `resolve.resolve_box`,
      `anchor.anchor_box`, `component_aggregate._revise`, and `partwhole` — where it also **fixes
      the genuine multi-commit hazard**: `_persist_direct` (per-proposition isolation preserved) and
      `_rebuild_closure` are each their own atomic unit instead of an unbracketed internal commit.
      Tests: `test_age_cypher.py` (commit-once / rollback-and-reraise on a mock); integration
      `test_atomic_write.py` (a failure injected *between* a graph write and the `Action` rolls both
      back — no orphaned vertex, no orphaned `Action`; the success direction commits both).
      **Remaining mechanical adoptions** (already single-commit / orphan-safe today — `atomic_write`
      only adds the rollback-on-mid-failure hardening, identical pattern): `reference.bind_proposition`,
      `edge_producer.produce_edges` (keep its single-transaction-for-all-plans semantics on wrap),
      `reembed` (ORM-only). `proposition.py` already hand-rolls the discipline (per-span
      try/`_persist`/except-rollback) — the reference implementation `atomic_write` generalizes; the
      "caller owns transaction" operators (`domain/loader`, `boxes/registry`, `reference_corpus`,
      `ingest`) correctly defer the commit to a caller that should wrap with `atomic_write`.
- [ ] **W8 — Cypher chokepoint + serde round-trips** *(assessment, P10)*. ~140
      call sites interpolate labels/edge types/ids/timestamps into f-string
      Cypher outside the `db/age.py` helpers — safe today (values come from
      enums/UUIDs/`isoformat()`), but convention, not construction: one future
      call site with a user-influenced value breaks it silently. (1) A thin
      query-builder over `db/age.py` (validated label/edge enums, mandatory
      value escaping through the `cypher_map` machinery); migrate the writers;
      a CI grep gate against raw f-string Cypher outside it. (2) Round-trip
      property tests for every manual serde pair (`Sensitivity.flatten` /
      `from_props`, `SourceInterest`, box serde, `same_as_to_props`) so a
      write/read format drift cannot ship without a test going red.
- [ ] **W11 — small hardening batch** *(assessment, minor findings — one PR)*:
      embedding-model identity checked at substrate construction, not first
      write (fail before the expensive re-embed, `core/embeddings.py`);
      verifier-down degraded mode surfaced as a triage-visible reason, not only
      a log line; the segment `Action` records the per-reason span-skip split
      (whitespace vs zero-vector); `DEFAULT_AGREEMENT_THRESHOLD` becomes a
      config knob; `types/nodes.py` gains the `Mention` placeholder (or an
      explicit docstring pointer) so the §3.1 binding seam is visible in the
      schema module, not only in `core/reference.py` prose.

## Conventions for executing agents *(applies to every task in every phase file)*

- Run everything via the project venv: `.venv/bin/python` / `uv run` — never bare
  `python3`.
- Tests: `uv run pytest tests/unit -x` for unit; integration needs the ephemeral DB
  (see `MIGRATIONS.md`); if you cannot run integration tests, say so — never claim
  them green.
- Do not start Docker containers without explicit approval.
- One task per PR; reference the task id (G/R/V/W) in the PR body.
- `architecture.md` is the source of truth; if a task seems to contradict it, stop
  and report instead of improvising.
- Migrations: set `down_revision` to the current head (`alembic heads`) — revision
  numbers written inside older task specs are stale. Read `CI_MIGRATIONS.md` before
  writing any migration (the AGE search_path hazard).
- After ruff autofix, re-check that imports you added are still present (the
  PostToolUse hook can strip an import whose use lands in a later edit).
- Before starting any task, check the **deferred-items trigger table** above; state
  in the PR body which triggers (if any) your change fires.
