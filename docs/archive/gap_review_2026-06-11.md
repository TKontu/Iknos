# Gap Plan — Review Remediation (2026-06-11, post-Phase-4-core)

**Why this file exists.** The 2026-06-11 review
(`review_2026-06-11_post_phase4_review.md`) found that the June code defects are
fixed but the project's critical path has shifted: **the validation gate is the
next milestone and none of its assets exist** (F1), **two safety gates are
unenforced at the now-live REFUTES creation site** (F2), and several smaller
items (H2–H4, M1–M2). This file holds the implementation tasks. Each task is
written to be executable by an agent without further context: read only the
files named in the task, follow the spec, satisfy the acceptance criteria, add
the named tests.

**Relationship to other plans.** `gap_review_2026-06.md` (the June R-tasks)
remains live for **R4, R8, R9, R10, R11, R12, R13** — V-tasks below that depend
on an R-task say so explicitly. The trial *definitions* stay in
`todo_trials.md`; this file is the granular work breakdown for the trial
*assets* (A0 corpus, labels, harness, E1 baselines). Where a V-task and a trial
doc overlap, the trial doc defines *what to measure*, the V-task defines *what
to build*.

**Conventions for executing agents (read first):**

- Run everything via the project venv: `.venv/bin/python` / `uv run` — never
  bare `python3`.
- Tests: `uv run pytest tests/unit -x` for unit; integration tests need the
  ephemeral DB (see `MIGRATIONS.md`); if you cannot run integration tests, say
  so — do not claim them green.
- Do not start Docker containers without explicit approval.
- One task per PR. Branch name `gate/v<N>-<slug>` (or `fix/v<N>-<slug>` for
  V7–V11). Reference this file and the task id in the PR body.
- `architecture.md` is the source of truth; if a task seems to contradict it,
  stop and report instead of improvising.
- After ruff autofix, re-check that imports you added are still present (the
  PostToolUse hook can strip an import whose use lands in a later edit).
- Before starting any task, check the **deferred-items trigger table in
  `todo.md`** — if your slice fires a trigger, say so in the PR body.

## Status / sequencing

| Task | Title | Severity | Gate | Status |
|------|-------|----------|------|--------|
| V1 | Gate corpus: planted documents + manifest | Critical | blocks G4.6, A0–A7, E1 | ☐ |
| V2 | Gate corpus: gold labels (edges, states, faithfulness, clusters, levels) | Critical | blocks G4.6; needs V1 | ☐ |
| V3 | Evaluation harness: metrics library + bias-controlled scoring | Critical | blocks G4.6; parallel with V1 | ☐ |
| V4 | E1 baseline 1: tuned plain-RAG rig | Critical | blocks E1 go/no-go | ☐ |
| V5 | E1 baseline 2: agentic / multi-hop RAG rig | High | blocks E1; after V4 | ☐ |
| V6 | E1 baseline 3: expert+search protocol (doc + recording template) | Medium | blocks E1; parallel | ☐ |
| V7 | Quarantine enforcement in the edge producer | Critical | before G4.5; needs R8+R9 | ☐ |
| V8 | `persist_verdicts` ensemble filter (the G4.5 consumer-filter slice; consumes `core/ensemble_gate.py` slice 1) | Critical | before remaining G4.5 slices | ☐ |
| V9 | pgvector k-NN push-down + recall-vs-exact measurement | High | with gate trials; needs R4 | ☐ |
| V10 | Architecture backports: credibility seam + anchor seam | Medium | doc-only, now | ☐ |
| V11 | Unit tests for `db/age.py` body path + provenance modules | Medium | opportunistic, before Phase 5 | ☐ |

Sequencing: V1 ∥ V3 ∥ V4 start immediately (independent). V2 follows V1 and is
the **longest-lead item** (human annotation — see V2's annotator note; start
recruiting on day one). V7 needs R8 then R9 (both in `gap_review_2026-06.md`)
— run them as the same work stream: R8 → R9 → V7 → V8. V9 needs R4. **R10/R11
(out-of-process embeddings, job queue) must land before the gate trials run on
the V1 corpus** — a real multi-document ingest as a synchronous foreground job
is the failure mode R11 exists to prevent. **Do not start the remaining G4.5
slices (channel producers, operators) before V7+V8** — note G4.5 *slice 1*
(`core/ensemble_gate.py`, the pure authoriser) shipped 2026-06-11 and V8 is its
consumer-filter slice. **Do not start Phase 5 work at all** (the gate decides
whether Phase 5 happens as designed).

---

## V1 — Gate corpus: planted documents + manifest

**Severity: Critical (every gate trial consumes this; nothing else can substitute).**

**Context.** Trial A0 (`todo_trials.md`) requires a small fixed corpus with
deliberately planted contradictions and a later overturning fact. The Phase 1
fixture corpus (`tests/fixtures/corpus/` — 3 documents, `manifest.toml`, a
typed loader in `tests/fixtures/`) is the *seed and the mechanism*, not the
corpus. This task authors the gate corpus as a sibling fixture set reusing the
same loader machinery. **Documents only in this task** — labels are V2, so the
two can proceed with different people/skills.

**Domain.** Industrial equipment failure (gearbox / bearing RCA) — the
architecture's running example (§14), so the entity hierarchy
(gearbox ⊃ shaft ⊃ bearing ⊃ roller) and failure-mode vocabulary are already
documented. All documents are plain text (`.txt`), written for this corpus
(no copyrighted material), each 300–3,000 words except where noted.

**Changes.**

1. New directory `tests/fixtures/gate_corpus/` with `manifest.toml` following
   the existing `tests/fixtures/corpus/manifest.toml` schema exactly (same
   keys; quoted anchors, never hand-counted offsets). Extend the existing
   loader only if it hardcodes the corpus path — prefer parameterizing the
   directory over duplicating loader code.
2. Author **10 documents** with this planted structure (track each planted
   item in the manifest under a `[[planted]]` table with a stable id, the
   anchor quote(s), and a `kind` field — V2's labels reference these ids):
   - `d01_incident_report.txt` — the case: gearbox failure incident report.
     Contains the central observations and at least one hard negation ("the
     operator stated the lubrication system had **not** been serviced") and
     one hedge ("possibly due to misalignment").
   - `d02_maintenance_log.txt` — maintenance history. Plants contradiction
     pair #1 with d01 (dates/actions that conflict).
   - `d03_supplier_analysis.txt` — the bearing supplier's failure analysis.
     Contains genuine **observations** (measurements, surface descriptions)
     AND a self-serving **judgement** (blames installation, §9.1) — the
     observation-vs-judgement split case.
   - `d04_oem_manual_excerpt.txt` — reference-tier: operating limits,
     lubrication intervals, the component hierarchy in prose (feeds `PART_OF`
     anchoring).
   - `d05_vibration_survey.txt` — instrument readings over time;
     **dissimilar refuter #1**: a routine reading whose value quietly rules
     out one live hypothesis without mentioning it (semantically far from the
     hypothesis text — this is the §5.1 dissimilar-refuter test case, the
     single most important planted item).
   - `d06_operator_interviews.txt` — testimony with attribution/reported
     speech, coreference hard cases ("the HSS bearing" / "bearing 3" / "it"
     all denoting one entity; plus a *different* bearing as the over-merge
     trap), and an **admission against interest**.
   - `d07_metallurgy_report.txt` — lab observations; **dissimilar refuter
     #2** (a material-composition finding that refutes the counterfeit-part
     hypothesis without using any of its vocabulary).
   - `d08_purchasing_records.txt` — long document, **must exceed one
     embedding window** (> 8,192 bge-m3 tokens; pad with realistic
     line-item tables, not lorem ipsum) with one load-bearing fact planted
     in the final 10% (the windowed-embedding regression anchor, G1.13).
   - `d09_industry_bulletin.txt` — reference-tier failure-mode bulletin
     (seeds the reference hypothesis set, §11.2).
   - `d10_followup_correction.txt` — the **overturning fact**: a later
     correction that retracts a key claim from d02, flipping the
     best-supported hypothesis (the §7.3/§8 retraction test). Must carry an
     explicit later date.
3. Plant **4 candidate hypotheses** (record their statements in the
   manifest): H1 lubrication failure (true cause), H2 installation error
   (the supplier's self-serving judgement supports it), H3 counterfeit
   part (refuted by d07), H4 overload/misuse (refuted by d05). Before d10,
   the evidence must favour H2; after d10, H1 — that flip is the gate's
   retraction measurement.
4. `tests/fixtures/gate_corpus/README.md` — one page: the scenario, the
   planted-item inventory by id, which trial consumes which item. Spoiler
   warning at top (annotators for V2 must label *before* reading it — see
   V2).
5. One smoke test `tests/unit/test_gate_corpus.py`: the loader loads all 10
   documents; every manifest anchor string occurs exactly once in its
   document; d08 exceeds the window length (assert on token count via the
   tokenizer the substrate already exposes, or character-count proxy ≥
   40,000 chars if the tokenizer is too heavy for unit tests — pick one,
   document it).

**Acceptance criteria.**
- [ ] 10 documents + manifest load through the existing loader machinery.
- [ ] Every planted item in the inventory has a manifest entry with a
      verified quote anchor.
- [ ] d08 exceeds one embedding window; d10 carries the latest date.
- [ ] No labels in this PR (V2's scope) — the manifest records *what was
      planted*, not *what the system should output*.

**Do not:** run any ingest/extraction on the corpus in this task; write
gold-output labels; use real company names.

---

## V2 — Gate corpus: gold labels + second annotator

**Severity: Critical (longest lead item in the project — start the human part on day one).**

**Context.** A0 requires gold labels with **≥ 2 annotators** for the
agreement-gated items (κ > 0.6 before level-attachment automation, §13). This
task defines the label formats, collects both annotators' labels, and computes
agreement. Depends on V1 (the documents and planted-item ids).

**Annotator logistics (the human-critical-path note).** This is a
single-developer project; the second annotator must be recruited (a colleague
or domain-adjacent engineer; 4–8 hours of labeling with the instructions
below). **Fallback if no second annotator is available after two weeks of
trying:** the developer labels twice with ≥ 14 days separation and no review
of the first pass; record this as a documented limitation in the trial report
(intra- rather than inter-annotator agreement — weaker evidence, better than
none). Either way the labeling happens *before* reading `gate_corpus/README.md`
(the planted-item inventory is the answer key; an annotator who has read it is
contaminated). The label *instructions* file (below) is safe to read.

**Changes.**

1. `tests/fixtures/gate_corpus/labels/INSTRUCTIONS.md` — self-contained
   annotator instructions (no architecture jargon): how to mark an
   evidence→hypothesis relation as supports/refutes/irrelevant; how to mark
   negation/modality/attribution on a flagged span; how to group mentions
   into entity clusters; how to attach a fact to a component level
   (gearbox/shaft/bearing/roller). Include 2 worked examples each.
2. Label files, one TOML per label family (schema documented at the top of
   each file; every row references V1 planted ids or `(document, quote)`
   anchors — never offsets):
   - `gold_edges.toml` — every planted SUPPORTS/REFUTES edge: evidence
     anchor, hypothesis id, sign, `dissimilar: bool`.
   - `gold_hypothesis_states.toml` — per hypothesis: state **before** d10
     and **after** d10 (the flip ground truth).
   - `gold_faithfulness.toml` — for ~30 flagged spans: the correct
     polarity/modality/attribution/epistemic-class values (A5).
   - `gold_entity_clusters.toml` — mention → cluster id, including the
     hard-case and trap mentions from d06 (A6).
   - `gold_levels.toml` — fact anchor → component level, **per annotator**
     (this is the κ-gated family; keep both annotators' columns).
3. Agreement computation: a small script `scripts/gate_agreement.py`
   (stdlib + the V3 metrics lib) that reads `gold_levels.toml` (and any
   other dual-annotated family), prints Cohen's κ per family, and exits
   non-zero if κ < 0.6 — the §13 automation gate, runnable by hand and in
   the trial harness.
4. Disagreements between annotators: reconcile by discussion, record the
   final adjudicated value in a `consensus` column, **keep** the original
   per-annotator values (the κ computation uses originals, the trials use
   consensus).

**Acceptance criteria.**
- [ ] All five label families exist, parse, and reference only anchors that
      resolve in the V1 documents (extend `test_gate_corpus.py` to verify
      resolution).
- [ ] `gold_levels.toml` carries two annotators' labels + consensus; κ
      computed and recorded in the file header with the annotation dates.
- [ ] The annotator-contamination rule (label before reading README) is
      stated in INSTRUCTIONS.md and the PR body confirms it was followed.

**Do not:** generate labels with an LLM (the gold standard must be
independent of the model family being evaluated — §8 bias-control); skip the
second-annotator attempt and jump to the fallback.

---

## V3 — Evaluation harness: metrics library + bias-controlled scoring

**Severity: Critical (the gate cannot be *measured* without it; also closes review M2 — calibration is otherwise unmeasurable).**

**Context.** A0's harness item. Pure measurement code — consumes V1/V2
fixtures and system outputs, never calls an LLM (§8: gold answers with
controlled ordering, never LLM-as-judge).

**Changes.**

1. New package `src/iknos/trials/` (importable so trials and tests share it):
   - `metrics.py` — pure functions, each with a docstring formula and a
     hand-computed example in its unit test:
     - `recall_at_budget(gold: set, candidates: list, budget: int) -> float`
       — used split by sign: supporter recall and **refuter recall**
       separately (A1).
     - `ece(predictions: list[tuple[float, bool]], bins: int = 10) -> float`
       and `brier(predictions) -> float` (A3, E1 calibration axis).
     - `reliability_diagram(predictions, bins) -> list[tuple[float, float, int]]`
       — (bin mean confidence, bin accuracy, n); the data behind the M2
       calibration check; no plotting, just the table.
     - `cohen_kappa(a: list, b: list) -> float` (V2, A4).
     - `spearman_rho(a: list[float], b: list[float]) -> float` (A4
       depth-recovery).
     - `state_flip_error(gold_before, gold_after, observed_before, observed_after) -> dict`
       — per-hypothesis: flipped-when-should, held-when-should,
       wrong-direction (the d10 retraction measurement).
   - `scoring.py` — the bias-control wrapper: given a list of system answers
     and gold answers, evaluate under **controlled ordering** (a fixed
     permutation schedule, seeded from the content hash of the item — reuse
     the `_permutation` pattern in `core/edge_judge.py`), so no metric can
     depend on presentation order. Assert (not document) that nothing in
     `src/iknos/trials/` imports `core/llm.py`.
   - `report.py` — render a metrics dict to a markdown table (the trial
     reports in `docs/trials/` will embed these).
2. No `Date.now`-style ambient state: every function takes its inputs
   explicitly; the package must be importable without `DATABASE_URL`.
3. Unit tests `tests/unit/test_trial_metrics.py`: each metric against a
   small hand-computed fixture (write the expected value in a comment with
   the arithmetic shown); κ edge cases (perfect agreement = 1.0, chance
   agreement ≈ 0.0); ECE on a perfectly calibrated synthetic set ≈ 0.

**Acceptance criteria.**
- [ ] All metrics named above implemented, unit-tested against hand-computed
      values.
- [ ] An import-graph test proves `iknos.trials` does not import the LLM
      client.
- [ ] `scripts/gate_agreement.py` (V2) can import `cohen_kappa` from here.

**Do not:** build trial *runners* (each trial wires its own inputs when it
runs — runners without V1/V2 data would be untestable scaffolding); add
plotting dependencies.

---

## V4 — E1 baseline 1: tuned plain-RAG rig

**Severity: Critical (the go/no-go is only as valid as the strongest baseline — a weak rig biases E1 toward the system).**

**Context.** Trial E1 (`todo_trials.md`) compares the system against a
baseline ladder on the *same* corpus and questions. This task builds the
first rung: a genuinely well-tuned plain-RAG pipeline. It must be a *fair
strong baseline*, not a strawman: same LLM endpoint, same embedding model,
sensible retrieval.

**Changes.**

1. New package `src/iknos/baselines/` with `rag.py`:
   - Chunking: fixed-size with overlap (512 tokens / 64 overlap) — standard
     RAG practice, deliberately *not* reusing the iknos segmentation (the
     baseline must be what a competent team would build *without* this
     project).
   - Index: reuse pgvector via a dedicated `baseline_chunks` table (own
     alembic migration, next free revision; table: id, document_id, text,
     embedding vector(1024), model) — same embedding model through the
     existing substrate seam, so the comparison isolates the *architecture*,
     not the embedder.
   - Retrieval: top-k cosine (k=8 default, configurable) + answer prompt:
     question + retrieved chunks → answer with citations to chunk ids.
     One LLM call, same `core/llm.py` client (it is the swappable seam —
     baselines may use project plumbing, just not project *reasoning*).
   - Output contract (shared by V4/V5/V6 so V3 scores them identically):
     `BaselineAnswer {question_id, answer_text, cited_chunk_ids,
     confidence: float}` — confidence is the model's verbalized 0–1 (that
     *is* the baseline's calibration story; do not multi-sample it — that
     would be importing the system's discipline into the baseline).
2. A thin runner `scripts/run_baseline.py --baseline rag --corpus
   tests/fixtures/gate_corpus --questions <toml>` that ingests the corpus
   into `baseline_chunks`, answers each question, writes
   `docs/trials/baseline_rag_answers.toml`. (Questions file format: id +
   question text + the hypothesis it probes; authored in V1's manifest or a
   sibling `questions.toml` — add it here if V1 didn't.)
3. Tests: unit-test chunking boundaries and the answer-assembly prompt
   construction (mock LLM, mirror `tests/unit/test_llm.py` mock style);
   integration-test ingest+retrieve on 2 small fixture docs.

**Acceptance criteria.**
- [ ] End-to-end: corpus in, per-question answers with citations and
      confidence out, persisted to a TOML the V3 harness can score.
- [ ] No import of iknos segmentation/proposition/graph modules inside
      `baselines/rag.py` (import test) — plumbing yes, reasoning no.
- [ ] Tuning knobs (k, chunk size/overlap) are constructor params with the
      defaults above.

**Do not:** add a reranker or query rewriting (that is V5's multi-hop rig);
score the answers (V3's job, when V2 labels exist).

---

## V5 — E1 baseline 2: agentic / multi-hop RAG rig

**Severity: High.**

**Context.** Rung 2 of the E1 ladder: multi-hop retrieval with tool use —
the strongest cheap competitor. Builds on V4's table, runner, and output
contract.

**Changes.**

1. `src/iknos/baselines/agentic_rag.py`: an LLM-driven loop (max 6 steps)
   over two tools — `search(query) -> top-k chunks` (V4's retrieval) and
   `answer(text, citations, confidence)` (terminates). The LLM may
   reformulate queries, issue several searches, and must end with `answer`.
   Implement the loop directly on `core/llm.py` structured output (the
   project already does grammar-level structured output — reuse that
   pattern); no agent framework dependency.
2. Add `--baseline agentic` to `scripts/run_baseline.py`; same output
   contract and answers file (`baseline_agentic_answers.toml`).
3. Per-question trace (queries issued, chunks seen) persisted alongside the
   answer — E1's traceability axis is scored on *what the baseline can
   cite*, so the trace must be honest and complete.
4. Tests: unit with a scripted mock LLM (two searches then answer; loop cap
   enforced; malformed tool call → one retry then fail that question
   loudly, recorded as unanswered — never a silent skip).

**Acceptance criteria.**
- [ ] Runs end-to-end on the gate corpus within a bounded budget (≤ 6 LLM
      calls per question + 1 answer call).
- [ ] Traces persisted; unanswered questions explicit in the output file.

**Do not:** give it iknos's graph, propositions, or contradiction machinery
(then it would *be* the system); exceed the step budget.

---

## V6 — E1 baseline 3: expert+search protocol

**Severity: Medium (no code — a protocol document + recording template; cheap, do not let it block the others).**

**Context.** Rung 3: a human expert with good search. This is a *protocol*,
not software.

**Changes.**

1. `docs/trials/e1_expert_search_protocol.md`: who (the V2 second annotator
   or another colleague — **not** the developer, who knows the planted
   answers); the toolset (the corpus as plain files + ripgrep/editor search
   — no iknos); time box (e.g., 25 min per question); what they record per
   question (answer, the passages they relied on as citations, a 0–1
   confidence, time taken).
2. `docs/trials/e1_expert_answers_template.toml` matching the V4/V5 output
   contract so V3 scores all three rungs identically.
3. Add the contamination rule: the expert must not have read
   `gate_corpus/README.md` or the labels.

**Acceptance criteria.**
- [ ] Protocol is executable by a person with no iknos knowledge from the
      doc alone; template parses with the same schema as V4/V5 outputs.

---

## V7 — Quarantine enforcement in the edge producer

**Severity: Critical (review F2 — the §3.1 invariant has no enforcement at the live REFUTES creation site).**

**Context.** §3.1: a provisional proposition "must not drive a strong move
(e.g., a `REFUTES` that overturns a hypothesis)." `core/edge_producer.py`
(G4.3 slice 3) is the creation site for `SUPPORTS`/`REFUTES` edges and
currently never consults provisional state (verified 2026-06-11: no
reference to `provisional` or faithfulness filtering in
`edge_producer.py`/`edge_judge.py`). **Depends on R8 (`provisional_reasons`)
and R9 (`core/quarantine.py`, `assert_not_quarantined`) from
`gap_review_2026-06.md` — land those first, in that order.** Read
`edge_producer.py`'s module docstring and `plan_hypothesis` /
`build_evidence` / `produce` before changing anything; the design intent
(record-and-skip, never abort the batch) is below.

**Changes.**

1. **Load the reasons.** Where the producer resolves each evidence node's
   `statement` and `effective_credibility` (the `_load_node_meta` /
   credibility-load step), also resolve the node's **provisional reasons**:
   a Fact/Conclusion inherits the union of `provisional_reasons` over the
   `Proposition`s it is `EVIDENCED_BY`. One extra read in the same query
   pattern the statement load already uses; an evidence node with no
   proposition (shouldn't exist — §10 requires `EVIDENCED_BY`) is treated
   as quarantined with reason `"missing_provenance"` and logged at warning.
2. **Enforce at planning, record-and-skip.** In `plan_hypothesis`, after the
   judge returns and before an edge is planned: for each would-be edge,
   derive its stakes — `Stakes.HIGH` for any `REFUTES`, and for a `SUPPORTS`
   that would be the hypothesis's **sole** support in this plan;
   `Stakes.LOW` otherwise — and call `assert_not_quarantined(reasons,
   stakes)` (R9). On `QuarantinedPropositionError`: **drop the edge from the
   plan** (do not raise out of the batch — one quarantined edge must not
   abort the other hypotheses, matching the existing per-hypothesis
   isolation design) and record it in the hypothesis's `Action` outputs
   under a new `outputs.quarantined` list: `{evidence_id, sign, reasons,
   stakes}`. The quarantined pair is a triage signal (§11.1 consumes it
   later), not an error.
3. **Keep it pure where the module is pure.** The stakes derivation and the
   inherit-reasons union are pure helpers next to `edge_significance` /
   `build_evidence`; the DB read joins the existing load step. Follow the
   module's pure/DB split (the repo convention).
4. Docstring: extend the module's invariants section — "no `REFUTES` (or
   sole-support `SUPPORTS`) is persisted from a provisional evidence node;
   quarantined pairs are recorded on the Action, never silently dropped."

**Acceptance criteria.**
- [ ] A REFUTES candidate whose evidence node traces to a proposition with
      non-empty `provisional_reasons` is not persisted; the Action's
      `outputs.quarantined` records it with reasons.
- [ ] The same evidence node may still produce a LOW-stakes SUPPORTS
      (quarantine is stakes-gated, not a blanket ban).
- [ ] A sole-support SUPPORTS from provisional evidence is quarantined; the
      same SUPPORTS with a second non-provisional supporter in the plan is
      not.
- [ ] A quarantined edge never aborts the batch; other hypotheses' edges
      persist and commit.

**Tests.** Unit (`tests/unit/test_edge_producer.py`, extend): stakes
derivation table-test; plan-level quarantine drop with mocked reasons.
Integration (`tests/integration/test_edge_producer.py`, extend): persist a
provisional proposition → fact → run produce → assert no REFUTES edge in
AGE and the Action row carries `quarantined`.

**Do not:** raise out of `produce` on quarantine; filter at the candidate
or judge stage (the judge should still see the evidence — quarantine gates
the *write*, and the judged result is wanted for triage); change
`qbaf_adapter.py` (that is V8).

---

## V8 — `persist_verdicts` ensemble filter (refuted-flip lock)

**Severity: Critical (review F2 — §7.2's "never a single judgment" is currently a caller convention, not a mechanism).**

**Context.** §7.2: a flip to `refuted` requires the ensemble gate. **G4.5
slice 1 has shipped the gate's pure decision core** —
`core/ensemble_gate.py::authorise(signals, gate)`, unanimity-of-required +
dissent-veto, `DEFAULT_GATE` requiring `{LLM, SYMBOLIC}` (safe-by-default:
the symbolic channel is an ABSTAIN seam today, so no automated flip can be
authorised yet). But `core/qbaf_adapter.py::persist_verdicts` still "writes
what it's given, the caller filters" — the lock exists and nothing is
locked with it. This task is the **consumer-filter slice of G4.5** (already
named as open in `gap_phase_4_linking_adjudication.md`): make the invariant
structural inside the writer, so no caller can forget it. Read
`core/ensemble_gate.py` and `core/qbaf_adapter.py` plus the G4.4/G4.5
sections of `gap_phase_4_linking_adjudication.md` before changing anything.

**Changes** (in `core/qbaf_adapter.py`; the gate module is consumed, not
modified):

1. `persist_verdicts` gains a `gate_decisions: Mapping[str, GateDecision]`
   parameter (hypothesis id → the slice-1 `authorise` result; default empty
   mapping). For each verdict whose computed state is `refuted`:
   - if its hypothesis id maps to an **authorising** decision → persist as
     today;
   - otherwise (no entry, or a non-authorising/withheld decision) →
     persist the `acceptability` as computed, persist `state` as the
     hypothesis's **previous** state (read it in the same query; if the
     node has no prior state, `unsupported`), and set
     `pending_refutation: true` on the Hypothesis vertex. Clear
     `pending_refutation` whenever a verdict for that hypothesis is later
     persisted as non-refuted or as authorised refuted.
2. Record the hold: follow the adapter's existing audit behavior (check
   whether it writes Actions; if not, a warning-level structured log) with
   the held-back hypothesis ids and `reason: "ensemble_gate_pending"`. A
   withheld flip is a §13 finding ("surfaced, never smoothed") — it must
   be visible, and it is the same `is_finding` notion the gate's
   `GateDecision` already carries; reuse that field rather than inventing
   a parallel one.
3. Docstrings on both the function and module: the lock's contract —
   "`refuted` is unreachable through this writer without an authorising
   `GateDecision`; `core/ensemble_gate.py::authorise` is the only intended
   producer."
4. One-line architecture backport (same change, included here): in
   `architecture.md` §7.2, append to the ensemble-gate sentence:
   *"Enforced structurally: the QBAF persistence layer refuses a
   `refuted` state without an authorising gate decision and holds it as
   `pending_refutation` — a surfaced finding, not a silent flip."*

**Acceptance criteria.**
- [ ] With no gate decision, a refuted-band verdict persists acceptability
      but not the `refuted` state; `pending_refutation=true` on the vertex;
      prior state retained.
- [ ] With an authorising `GateDecision` for the hypothesis, behavior is
      identical to today.
- [ ] A subsequent supported/unsupported (or authorised refuted) verdict
      clears `pending_refutation`.
- [ ] No other code path writes `Hypothesis.state` (grep `src/` for writes
      to the property and assert in the PR body).

**Tests.** Unit: the hold/authorise/clear state table (build
`GateDecision`s through the real `authorise` with fixture signals — do not
mock the gate). Integration (extend the existing G4.4 adapter test): full
evaluate→persist on live AGE with and without an authorising decision.

**Do not:** modify `core/ensemble_gate.py` (its policy/algebra is decided
and fixture-tested); build the symbolic/temporal channel producers (later
G4.5 slices); change `classify_state` (the *computation* is unchanged —
only what may be *persisted* is gated).

---

## V9 — pgvector k-NN push-down + recall-vs-exact measurement

**Severity: High (review H3 — §5.1's "sublinear, reuses the dense index" has no implementation and no owner; the in-memory exact scan cannot survive the reference corpus).**

**Context.** `core/candidates.py::embedding_knn_candidates` is exact
in-memory cosine, documented eyes-open as "the recall ceiling an
approximate pgvector ANN index is later measured against (G4.6), and the
seam where the `<=>` push-down replaces this loop." This task builds that
seam's other side. **Depends on R4** (`gap_review_2026-06.md` — the HNSW
migration + cosine-operator standardization; note R4's migration numbering
is stale, next free revision is ≥ 0013 — set `down_revision` to the actual
head, check `alembic heads`).

**Changes.**

1. In `core/candidates.py` (or the adapter, following the module's pure/DB
   split — the SQL lives with the other DB reads in
   `CandidateGenerationAdapter`): a DB-backed alternative to the in-memory
   stage —
   `knn_pushdown(session, hypothesis_vectors, k, model) -> list[Candidate]`
   issuing per-hypothesis
   `SELECT proposition_id FROM proposition_embeddings WHERE model = :model
   ORDER BY embedding <=> :vec LIMIT :k`, then mapping proposition →
   reasoning node via the same `EVIDENCED_BY` read the in-memory path uses.
   Same contract, same `CandidateSource.EMBEDDING_KNN`, same
   no-similarity-floor default, same model-identity guard (the `WHERE
   model =` clause *is* the guard — assert it is never dropped).
2. Selection: a new setting `CANDIDATES_KNN_PUSHDOWN: bool = False`
   (env-configurable, `config.py` + `.env.example`). **Default stays
   in-memory exact** until the measurement below says otherwise — flipping
   the default is a G4.6 decision, not this task's.
3. The measurement (the reason this task exists): an integration test
   `tests/integration/test_knn_pushdown_recall.py` that loads ≥ 200
   synthetic normalized vectors, runs both paths with identical inputs,
   and asserts (a) push-down results are a subset-or-equal ranking of the
   exact path at the same k on this small set, and (b) EXPLAIN on the
   push-down query contains `hnsw` (mirror R4's EXPLAIN assertion;
   `SET LOCAL enable_seqscan = off` for the tiny-table case). The *recall@k
   vs exact* number on the real gate corpus is then a one-line addition to
   the G4.6 run — note it in `todo_phase_4_linking_adjudication.md`'s gate
   checklist (edit included in this task).
4. Record the distance-operator decision where the code makes it: a
   comment on the query — "`<=>` cosine distance; vectors L2-normalized;
   must match the R4 index opclass (`vector_cosine_ops`) or the index is
   not used."

**Acceptance criteria.**
- [ ] Both paths produce identical candidate sets on a small dense fixture
      (k < corpus size, no ties broken differently — reuse the in-memory
      path's deterministic tie-break, descending similarity then node id).
- [ ] EXPLAIN shows the HNSW index is used through the actual query path.
- [ ] Default behavior unchanged (`CANDIDATES_KNN_PUSHDOWN=false`).

**Do not:** flip the default; remove the in-memory path (it is the recall
ceiling / oracle); touch `funnel` or the structural stage.

---

## V10 — Architecture backports: credibility seam + anchor seam

**Severity: Medium (doc drift — `architecture.md` calls itself the source of truth and is missing two shipped decisions).**

**Context.** Review H4. Two decisions live only in code docstrings /
`HANDOFF.md` (which is gitignored and replaced per session — it is not a
record):

1. **Credibility routing (G4.3 slice 3).** The judge runs at identity
   reliability; `significance = SignificancePolicy.tier_weight(tier) ×
   effective_credibility`; strength stays the pure connection judgment.
2. **Taxonomy-anchor seam (G2.8 slice 2).** The §3.1 binding cascade's
   taxonomy stage shipped in the reference binder via G2.8's
   entity-linking, not as part of G2.4.

**Changes** (doc-only; quote-match the insertion points before editing):

1. `architecture.md` §10, the `significance` bullet (currently
   "**`significance`** — weight of the evidence if true, in [0, 1]: largely
   inherited from the evidence node's source/tier (§9), so it barely
   depends on LLM judgment."): append — *"Concretely (decided G4.3):
   `significance = tier_weight(tier) × effective_credibility` (§9.1); the
   §8 judge runs at identity source-reliability so `strength` stays the
   pure connection judgment — credibility enters here, never the strength
   discount. `tier_weight` is uniform 1.0 until the §8 experiment
   calibrates it."*
2. `architecture.md` §8, the confidence-pipeline decision list item "(3)
   encode each judgment as a subjective-logic opinion with
   source-reliability discounting": append — *"(source-reliability
   discounting is routed into edge `significance` per §9/§10, not applied
   inside the judge — see §10.)"*
3. `gap_phase_4_linking_adjudication.md`: confirm the slice-3 section
   records the same decision (the handoff says it does — verify, and add
   the §10 cross-reference if absent).
4. `gap_phase_2_graph_construction.md`, G2.4 section: one clarifying line —
   *"The §3.1 cascade's taxonomy-anchor stage shipped with G2.8 slice 2
   (the entity-linking fold), not as a G2.4 deferral — the cascade is
   complete as of G2.8 slice 2 except the pronoun/local-discourse stage."*
5. `architecture.md` §3.1, the binding-cascade sentence: no change needed
   unless the wording claims the cascade is unimplemented — read it; if it
   is purely normative (it is, per the 2026-06-11 review's read), leave it.

**Acceptance criteria.**
- [ ] All quoted insertions present; no other architecture text changed.
- [ ] `grep -n "tier_weight" docs/architecture.md` returns the new §10
      text.

**Do not:** restructure sections; "improve" adjacent prose; touch the
schema beyond the quoted additions.

---

## V11 — Unit tests for `db/age.py` body path + provenance modules

**Severity: Medium (review M1 — the audit-trail writers and the Cypher builder body path have no direct tests; auditability is principle 9).**

**Context.** `cypher_map` *is* unit-tested (`tests/unit/test_age_cypher_map.py`
— check it first; do not duplicate). Untested directly: the `cypher()` body
path (`_dollar_quote_tag` escalation, graph-name validation, the assembled
SQL shape), `provenance/action_log.py`, `provenance/audit.py`,
`boxes/registry.py`, `domain/loader.py`/`pack.py`, `config.py`.

**Changes** — new unit test files, pure where the module is pure; for DB
modules test the *query/statement construction and validation logic*, not
the round-trip (integration tests own round-trips):

1. `tests/unit/test_age_cypher.py`: `_dollar_quote_tag` returns the base
   tag for a clean body and escalates when the body contains it (include a
   body containing the *escalated* tag too); the assembled statement
   contains the tag exactly twice and no bare `$$` wrapper; graph-name /
   identifier validation rejects hostile values (read the module first —
   test what exists, and if graph-name validation does *not* exist, add
   the test as the spec and the one-line guard with it, per R3's original
   item 2).
2. `tests/unit/test_action_log.py`: the Action record construction —
   required fields present (`actor`, `action_type`, `inputs`, `outputs`),
   JSON-serializable inputs/outputs, timestamp handling; whatever pure
   seam exists (extract one if the module is all-DB: a
   `build_action(...) -> Action` helper is an acceptable minimal refactor,
   keep behavior identical).
3. `tests/unit/test_audit.py`: the provenance reach-back query/assembly
   logic for whatever pure parts exist (same extraction rule as above).
4. `tests/unit/test_boxes_registry.py` + `tests/unit/test_domain_loader.py`:
   registry serde round-trip (pydantic), loader behavior on a minimal
   in-memory pack fixture, loader error on malformed pack (missing
   taxonomy node referenced by an edge — or whatever the loader's actual
   invariants are; read it first).
5. `tests/unit/test_config.py`: defaults load without env; each setting
   added by recent gap work (`LLM_*`, `PARSER_*`, windowing params) reads
   from env; no DB touched on import.

**Acceptance criteria.**
- [ ] Every listed module has a dedicated unit-test file exercising its
      pure logic; refactors (if any) are extract-only with identical
      behavior.
- [ ] `uv run pytest tests/unit -x` green without `DATABASE_URL`.

**Do not:** convert integration coverage to mocks; chase coverage numbers
into the ORM/type modules (declarative code; the review explicitly scoped
those out).

---

## Explicitly out of scope for this file

- **G4.5 / G4.6 implementation** — owned by
  `gap_phase_4_linking_adjudication.md`; gated behind V7+V8 (and G4.6
  behind V1–V3).
- **R-tasks** — `gap_review_2026-06.md` remains their home; its status
  table was reconciled 2026-06-11.
- **Phase 6/7 entry criteria** (API design, authn/z, deployment runbook,
  UI stack) — added to `todo_phase_6_investigation_runtime.md` /
  `todo_phase_7_expert_interface.md` directly.
- **The deferred-items trigger table** — lives in `todo.md` (checked per
  PR; see conventions above).
