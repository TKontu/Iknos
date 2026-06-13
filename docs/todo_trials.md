# Iknos — Trial & Experiment Plan (pre-implementation gates)

These trials de-risk the open empirical details (architecture.md Open items + §13)
**before** the related production code is built or hardened. Each trial runs on a thin
slice, has an explicit decision threshold, and **gates** named production tasks: do not
harden the gated tasks until the trial's decision is made.

The trials fall into four instruments. Instrument A's trials all share one asset — the
planted-corpus + evaluation harness (Trial A0) — so build that first; it later doubles
as the regression suite. Trials flagged **⚠ may force redesign** can change the
architecture, not just a parameter; resource those earliest.

Build philosophy (from `todo.md`): thin slice → validate at gate → harden. Trials are
that validation. Evaluation is **bias-controlled** throughout — gold answers with
controlled ordering, never LLM-as-judge headline scores (§8, §13).

---

## Instrument A — Planted-corpus accuracy experiment

Resolves the accuracy-bound open details. One corpus, four trials. This is the
**Phase-4 validation gate**; passing it gates the hardening of Phases 3 and 4.

### Trial A0 — Build the planted-corpus and evaluation harness *(shared prerequisite — **start now**)*

**Scheduling (2026-06 review, P1).** A0 is the critical-path asset: every gate
(A1–A7, B2, E1) consumes it, and gold labelling with ≥2 annotators is **weeks of
work, not days** — yet it depends on *nothing unbuilt* (it is documents + labels +
scoring code). Run it as its own managed work item **in parallel with the Phase 1
tail**, starting immediately; do not let it materialize lazily when Phase 4 needs
it. Treat the sub-items below as the work breakdown; corpus authoring and labelling
can proceed while harness code is written. The Phase 1 fixture corpus is the seed.
Include at least one document longer than one embedding window and spans with
hard negation/modality cases (regression anchors for G1.13/G1.14).

> **Escalated (2026-06-11 review, F1).** The parallel-with-Phase-1 scheduling was
> not executed; Phase 4's core has now shipped and A0 **is the critical path**.
> Execute the agent-executable work breakdown below (**V1** corpus, **V2** gold
> labels + second annotator, **V3** metrics harness) — the checkboxes in this
> section are satisfied by V1–V3 landing; do not duplicate work.

- [ ] Assemble a small fixed corpus: sources with deliberately planted contradictions
      plus a later **overturning fact**.
- [ ] Plant a labeled set of ground-truth `SUPPORTS` **and** `REFUTES` edges (including
      *dissimilar* refuters — semantically far from their target).
- [ ] Label ground-truth hypothesis states and a human fact→referent **level** labelling
      (for A4), with ≥2 annotators to compute agreement.
- [ ] Label ground-truth **faithfulness** items (for A5): gold reference bindings, and
      negation/modality/attribution annotations on a sample of spans.
- [ ] Label ground-truth **entity clusters** (for A6): which mentions denote the same
      entity, including hard cases ("the HSS bearing" / "bearing 3" / "it").
- [ ] Build the evaluation harness: bias-controlled scoring (gold answers, randomized/
      controlled ordering), and a metrics library — recall@budget (split supporter vs
      refuter), ECE/Brier, hypothesis-state-flip error, Cohen's κ, Spearman ρ.
- [ ] Keep the corpus + harness as the permanent regression suite.
- **Gates:** all of A1–A7; feeds B2.

#### A0 work breakdown — V1/V2/V3 + V13 *(merged from `archive/gap_review_2026-06-11.md`; V13 from `archive/review_2026-06-12_completed_scope_residuals.md`; one task per PR, branch `gate/v<N>-<slug>`)*

**V1 — gate corpus: planted documents + manifest.** New
`tests/fixtures/gate_corpus/` with a `manifest.toml` following the existing
`tests/fixtures/corpus/` schema exactly (quoted anchors, never offsets); prefer
parameterizing the existing loader's directory over duplicating it. Domain:
gearbox/bearing RCA (§14's running example). **10 authored plain-text documents**
(300–3,000 words except d08), each planted item tracked under a `[[planted]]`
manifest table (stable id + anchor quote(s) + `kind`):
d01 incident report (hard negation + a hedge); d02 maintenance log (contradiction
pair #1 vs d01); d03 supplier analysis (genuine observations + a self-serving
judgement, §9.1); d04 OEM manual excerpt (reference tier; component hierarchy in
prose); d05 vibration survey (**dissimilar refuter #1** — a routine reading that
quietly rules out a live hypothesis without naming it; the §5.1 test case);
d06 operator interviews (attribution/reported speech; "the HSS bearing"/"bearing
3"/"it" one entity + a different bearing as the over-merge trap; an admission
against interest); d07 metallurgy report (**dissimilar refuter #2** vs the
counterfeit hypothesis, none of its vocabulary); d08 purchasing records (**> one
embedding window**, >8,192 bge-m3 tokens of realistic line items, a load-bearing
fact in the final 10%); d09 industry bulletin (reference hypothesis set, §11.2);
d10 follow-up correction (**the overturning fact** — retracts a key d02 claim,
explicit later date). **4 hypotheses** in the manifest: H1 lubrication failure
(true cause), H2 installation error (favoured *before* d10), H3 counterfeit part
(refuted by d07), H4 overload (refuted by d05) — the pre/post-d10 flip is the
retraction measurement. Plus `gate_corpus/README.md` (scenario + planted
inventory, spoiler warning) and a smoke test (loader loads all 10; every anchor
occurs exactly once; d08 exceeds the window). **No labels in V1** (V2's scope);
no real company names; no ingest/extraction runs.

**V2 — gold labels + second annotator** *(longest-lead item in the project —
start recruiting on day one; depends on V1)*. Labeling happens **before** the
annotator reads `gate_corpus/README.md` (it is the answer key);
`labels/INSTRUCTIONS.md` (jargon-free, 2 worked examples per family) is safe.
Fallback if no second annotator after two weeks of trying: the developer labels
twice ≥14 days apart without reviewing pass 1 — documented as a limitation
(intra- not inter-annotator). One TOML per family, rows referencing planted ids
or `(document, quote)` anchors: `gold_edges.toml` (evidence anchor, hypothesis,
sign, `dissimilar` flag); `gold_hypothesis_states.toml` (state before/after d10);
`gold_faithfulness.toml` (~30 spans: polarity/modality/attribution/epistemic
class — A5); `gold_entity_clusters.toml` (mention → cluster incl. the d06 traps —
A6); `gold_levels.toml` (fact → component level, **per annotator**, the κ-gated
family). `scripts/gate_agreement.py` prints Cohen's κ per dual-annotated family,
exits non-zero below 0.6 (the §13 automation gate). Disagreements reconciled into
a `consensus` column, originals kept (κ uses originals, trials use consensus).
**Never generate labels with an LLM** (§8 bias control). Extend the V1 smoke test
to verify every label anchor resolves.

**V3 — evaluation harness: metrics + bias-controlled scoring** *(parallel with
V1; also the calibration measurement the gate needs)*. New package
`src/iknos/trials/`, importable without `DATABASE_URL`, **never calls an LLM**
(assert via an import-graph test that it does not import `core/llm.py`):
`metrics.py` pure functions, each unit-tested against a hand-computed fixture —
`recall_at_budget` (used split by sign: supporter vs **refuter** recall, A1),
`ece` + `brier` (A3/E1), `reliability_diagram` (bin confidence/accuracy/n —
no plotting), `cohen_kappa` (V2/A4), `spearman_rho` (A4 depth recovery),
`state_flip_error` (per hypothesis: flipped-when-should / held-when-should /
wrong-direction — the d10 measurement). `scoring.py`: evaluate under a fixed
content-hash-seeded permutation schedule (reuse the `_permutation` pattern in
`core/edge_judge.py`) so no metric depends on presentation order. `report.py`:
metrics dict → markdown table. **No trial runners** (each trial wires its own
inputs when V1/V2 data exists); no plotting dependencies.

**V13 — gate-corpus touch-ups *before* the V2 labeling freeze** *(2026-06-12
residual review, finding 8 — `archive/review_2026-06-12_completed_scope_residuals.md`;
**must land before V2 labeling starts** — once an annotator has read the documents
the corpus is frozen and these become recorded limitations instead)*. One PR:
(a) **d07 dissimilar-refuter purity**: the second planted anchor quote ("No material
or heat-treatment non-conformance was found.", `manifest.toml:283` /
`d07_metallurgy_report.txt:23`) lexically matches H3's phrasing ("material or
manufacturing non-conformance", d09/manifest) — rephrase the sentence so it refutes
without H3's vocabulary (the first anchor, the 100Cr6 conclusions sentence, already
satisfies dissimilarity; match its register), and update the manifest quote.
(b) **d05 chunk-level honesty**: the README claims d05 excludes overload "without
using the words load / overload / rating", which holds for the anchor sentence but
not at chunk granularity (section header "3. Duty and loading" directly above the
anchor; "load history" in §6; "torque … transient" in the anchor's own paragraph —
near-verbatim H4/d09 mode-3 phrasing). Either rework the surrounding text so a
heading-inclusive chunk stays vocabulary-clean, or scope the README/manifest claim
to anchor-level — decide which preserves the §5.1 measurement better and say so in
the README inventory. (c) **d02 word floor**: 297 words vs the spec's 300 — pad
minimally without touching any anchor sentence. (d) **labels/INSTRUCTIONS.md
priming**: the §2 worked example ("overload … a later survey shows the load was
normal → false") is the only one reusing a *real corpus hypothesis* — swap it for a
fictional-domain example (pump/tank/guard, like the others). **Every anchor must
still resolve exactly once** — the gate-corpus smoke tests are the acceptance
criterion; if an anchor quote changes, change manifest and document together.

### Trial A1 — Candidate-generation recall (esp. refuter recall)  ⚠ may force redesign

- [ ] Build a thin candidate-generation funnel (§5.1): structural priors → embedding
      k-NN → coarse-to-fine.
- [ ] **Vary:** k for embedding k-NN, the coarse-to-fine cutoff, structural-prior breadth.
- [ ] **Measure:** recall of planted edges at a given candidate budget, **separately for
      supporters and refuters**, vs adjudication cost.
- [ ] **Decision:** pick the smallest budget that recalls ≥ target% of planted
      **refuters** (the binding constraint — a missed refuter is a silent false negative).
- [ ] **Redesign trigger:** if similarity + entity/topic generation still cannot recall
      refuters, contradiction-finding must become a dedicated pass over the hypothesis
      neighbourhood, not a funnel-gated step.
- **Gates:** Phase 4 — candidate generation; Phase 6 — generate-candidates stage.

### Trial A2 — LLM→QBAF weight mapping

- [ ] Implement candidate mappings from a calibrated sign+strength judgment to a QBAF
      base score / edge weight: linear, calibrated-logistic, subjective-logic-opinion→scalar.
- [ ] **Vary:** the mapping family and its parameters.
- [ ] **Measure:** hypothesis-state accuracy and calibration of final acceptability vs
      ground truth.
- [ ] **Decision:** the mapping minimizing state-flip error + calibration error.
- **Gates:** Phase 4 — QBAF adjudication; the §8↔adjudication seam before hardening Layer B.

### Trial A3 — Confidence calibration (consistency vs verbalized)

- [ ] Compare verbalized confidence, multi-sample consistency, and post-hoc calibration
      against held-out correctness; fit a per-model calibration map.
- [ ] **Measure:** ECE / Brier per method, per model.
- [ ] **Decision:** adopt the lowest-calibration-error method; persist the per-model map.
- **Gates:** Phase 4 — confidence fusion; Phase 3 — Layer B confidence inputs.

### Trial A4 — Part-whole acquisition & fact→level accuracy  ⚠ may force redesign

- [ ] On a labeled domain corpus, measure: entity-linking accuracy to the domain pack;
      induced-meronymy precision/recall; fact→level attachment vs human labels.
- [ ] **Measure:** linking accuracy, meronymy P/R, attachment agreement κ, anchoring
      coverage (fraction of referents that anchor), inferred-level depth-recovery ρ.
- [ ] **Decision gates:** automate level attachment only if **κ > 0.6**; trust
      inferred-level (box/ConE) embeddings only if **ρ > 0.6**; otherwise human-review-gate.
- [ ] **Redesign trigger:** anchoring coverage < ~50% in a domain → the pack is
      inadequate; invest in the taxonomy rather than leaning on text induction.
- **Gates:** Phase 2 — part-whole hierarchy automation; Phase 6/7 — level-based views.

### Trial A5 — Extraction faithfulness & reference binding  ⚠ may force redesign

- [ ] On a labeled corpus, measure: proposition-vs-span **entailment** accuracy;
      **negation/modality/attribution preservation** accuracy; **epistemic-class**
      (observation/testimony/judgement) classification accuracy; **coreference / reference
      binding** accuracy against gold (including "it" / "bearing 3" definite descriptions).
- [ ] **Vary:** single-pass vs multi-sample extraction; LLM-only vs dedicated coref
      model + entity linking; verification model choice; the stakes-dependent quarantine
      threshold.
- [ ] **Measure:** faithfulness gate metrics above + the rate of provisional/quarantined
      propositions and how many survive expert review.
- [ ] **Decision:** adopt the extraction config meeting the faithfulness bar; set the
      quarantine threshold so high-stakes moves are not driven by low-faithfulness atoms.
- [ ] **Redesign trigger:** if faithfulness (esp. negation/modality preservation or
      reference binding) cannot reach the bar, the proposition layer needs a stronger
      structured-extraction or coref subsystem before any reasoning layer is trusted —
      this is the foundation; everything downstream inherits its errors.
- **Gates:** Phase 1 — propositionization; Phase 2 — reference binding & extraction;
  precedes trusting *any* downstream reasoning trial (A1–A4 assume faithful atoms).

### Trial A6 — Entity resolution (merge/split quality)  ⚠ may force redesign

- [ ] On the labeled corpus (gold entity clusters), measure pairwise/cluster resolution
      **precision** (over-merge control) and **recall** (under-merge control), separately
      — they are asymmetric and gated differently.
- [ ] **Vary:** the auto-merge confidence bar; relational-vs-similarity scoring; with vs
      without taxonomy anchoring; continuous re-resolution cadence.
- [ ] **Measure:** precision/recall, fragmentation rate (under-merge), false-contradiction
      rate from over-merge, and whether the contradiction→split-review loop recovers
      over-merges.
- [ ] **Decision:** set the auto-merge bar to favour the conservative (under-merge)
      default; verify merge/split propagate correctly as belief revision (with Phase 3).
- [ ] **Redesign trigger:** if resolution precision/recall can't reach the bar, anchoring
      and abstraction level (A4) inherit the error — strengthen blocking/scoring or lean
      harder on taxonomy anchoring + expert review before trusting downstream reasoning.
- **Gates:** Phase 2 — entity resolution; bounds A4 (anchoring/level) and feeds the
  quality of every component-level aggregation (Phase 3).
- *Runnable now:* the subsystem under test shipped as **G2.3** (`core/resolve.py`) — the
  "Vary" knobs map directly: the auto-merge bar is `RESOLVE_CONFIRM_BAR`/`RESOLVE_CANDIDATE_BAR`,
  and relational-vs-similarity scoring is the deterministic relational `score_pair` (similarity
  barred from scoring, blocking-only). Taxonomy-anchoring and the contradiction→split-review
  loop are still deferred seams (G2.4/G2.5, Phase 4), so the first A6 pass measures the
  no-anchor, no-recovery-loop configuration.

### Trial A7 — Review-triage value of information (efficiency)

- [ ] On the planted corpus with a **simulated oracle reviewer** (a "review" reveals the
      ground-truth value of an item), order review by VoI vs baselines (random,
      centrality-only, confidence-only).
- [ ] **Vary:** the leverage/uncertainty/significance weighting; structural-proxy vs
      QBAF-perturbation leverage; batch re-rank cadence.
- [ ] **Measure:** **reviews-to-correct-conclusion** (how many items must be reviewed
      before the ranked hypotheses are correct/stable) under each ordering; and whether
      planted errors surface near the top.
- [ ] **Decision:** adopt VoI ordering if it reaches the correct conclusion in materially
      fewer reviews than the cheaper baselines.
- [ ] **Justify-the-complexity trigger:** if VoI does not beat centrality-only or
      confidence-only, use the cheaper ordering — do not ship machinery that does not pay
      for itself.
- **Gates:** Phase 6 — triage; Phase 7 — review queue.

---

## Instrument B — Human-judgment studies

Quality here is a human judgment; metrics alone are insufficient. Run on the thin
slice before building the corresponding interface.

### Trial B1 — Mixed-level frontier (significance-weighted DoI)  ⚠ may force redesign

- [ ] Implement the frontier on the degree-of-interest framework (Furnas; van Ham &
      Perer), with **"evidence significance − distance"** replacing a-priori importance,
      weighted by §6 significance signals.
- [ ] **Vary:** the significance weighting and the display/token budget.
- [ ] **Measure (expert + management-proxy preference study):** does the frontier surface
      the genuinely most significant findings vs uniform-level baselines, without
      overwhelming? Significance-coverage and preference rate.
- [ ] **Decision:** adopt if it beats the uniform-level baseline on preference and
      significance-coverage. Most likely to need several iterations.
- [ ] **Redesign trigger:** if it cannot beat baseline, the significance signal needs
      redefining (or the frontier abandoned for a simpler semantic-zoom).
- **Gates:** Phase 6 — mixed-level frontier; Phase 7 — abstraction-level controls.

### Trial B2 — Cyclic-region detection & presentation

- [ ] Build deliberately cyclic argument fixtures (mutual support, circular refutation).
- [ ] Run the gradual semantics with an iteration cap; implement oscillation detection
      (e.g., strength variance over the last n iterations > ε).
- [ ] **Measure:** does it flag cycles vs falsely converge; and a small comprehension
      check — do experts read the "unresolved region" presentation correctly?
- [ ] **Decision:** set the iteration bound + oscillation criterion empirically; choose
      the presentation experts interpret correctly (principle 8: surface, don't force
      convergence).
- **Gates:** Phase 4 — QBAF oscillation handling; Phase 6 — cyclic-region presentation.

---

## Instrument C — Scale / latency benchmarks

Decided by measurement against load. C1 is needed before hardening the Layer A↔B
interface; C2 is an explicit "revisit at scale," not MVP-blocking.

### Trial C1 — Re-evaluation trigger policy & incremental cost (eager vs lazy)

- [ ] Instrument both strategies on representative investigation edit-traces
      (sequences of add/change/retract).
- [ ] **Measure:** re-evaluation latency, staleness, recompute volume; the propagation
      bound's effect; and — separately — **symbolic re-propagation cost (no LLM)** vs
      **LLM re-inference calls**, confirming the latter only fire where VoI is above
      threshold (§6.1, §11.1).
- [ ] **Verify incrementality:** a change re-analyses only the delta-affected region; cost
      scales with the affected region, not the whole graph (non-exponential). Content
      cache hit-rate on unchanged content ≈ 100%.
- [ ] **Budget mode:** under a fixed LLM budget, VoI-ordered re-inference produces a
      usable conclusion with un-inferred regions flagged provisional.
- [ ] **Decision:** eager while interactive latency stays under budget on typical edits
      (the likely case); switch to lazy/bounded if eager re-eval exceeds the budget.
- **Gates:** Phase 5 — belief-revision triggers & budget mode; Phase 3 — Layer A↔B
  interface before hardening Layer B.

### Trial C2 — Truth-maintenance placement (in-Postgres vs DBSP)

- [ ] Benchmark in-Postgres Counting retraction-propagation latency as graph size and
      edit volume grow; find the size where it misses the latency SLA.
- [ ] **Decision:** stay in-Postgres while under SLA; migrate Layer A to DBSP/Feldera
      (Postgres CDC) when retraction latency crosses the threshold.
- [ ] **Note:** revisit at scale — do not block the MVP on this.
- **Gates:** the scale path for Phase 3 — Layer A placement.

### Trial C3 — Storage-engine viability under schema density  ⚠ may force engine change — **hard backstop: before Phase 5**

**Scheduling (2026-06 review, P2).** Originally parked with the scale trials —
too late: Phases 2–5 (continuous entity resolution, recursive retraction,
bitemporal supersession) build *directly* on AGE, so a failure discovered after
them maximizes rework. The benchmark needs no production code: generate a
**synthetic** graph at target density and measure. Days of work; de-risks the
single biggest potential architecture swap. It is now a **Phase 2 entry
criterion** (`todo_phase_2_graph_construction.md`), paired with the G0.R2
property-index migration (`archive/gap_phase_0_residual.md`).

**Scheduling amendment (2026-06-11 architecture assessment, W9).** The Phase 2
entry criterion was missed — Phase 2 core shipped with C3 still unrun. New hard
backstop: **C3 runs before Phase 5 starts** (now a Phase 5 entry criterion,
`todo_phase_5_temporal_revision.md`), and its query set grows to include the
shapes Phase 5 will actually emit: **edge-property filters**
(`MATCH ()-[r:SAME_AS {state: 'confirmed'}]->()` — today no index path exists;
edge-property GIN is a deferred-table item) and **bitemporal supersession
updates at re-scoring rates**, on the full 15–20-property vertex payload. The
W2 fixture graph (`todo_phase_4_*.md`) is a realistic shape source.

- [x] **Prerequisite:** the G0.R2 AGE property-index migration is merged — benchmark
      the indexed engine, and verify with `EXPLAIN` through the real `cypher()` call
      path that the indexes are actually *used* (existence ≠ use). *(Migration `0007`;
      verified — see Result below.)*
- [x] Benchmark **Apache AGE** under the *real* schema density — every node/edge carrying
      provenance, two annotations, sensitivity, conditional credibility, bitemporal
      validity, overrides — at investigation scale (tens of docs) + a static reference
      base, on the actual query patterns (box-scoped retrieval, `WITH RECURSIVE` closure,
      SCC detection, bitemporal as-of, **and MERGE-by-id at entity-resolution call
      rates**). *(30 000-vertex / 48 993-edge synthetic graph, 18-property payload.)*
- [x] **Scope bitemporality:** confirm it is applied where needed (boxes, overrides,
      packs) and not bloating every `SAME_AS` edge. *(SAME_AS carries `state`/validity but
      no edge-property index — its absence is the one measured cost, see below.)*
- [x] **Decision:** stay single-engine (Postgres + AGE) if it meets latency; the fallback
      is a separate graph store at the cost of single-engine simplicity (§6, §13).
      **→ STAY single-engine.**
- **Gates:** Phase 2 entry (and the original Phase 0 engine commitment, retroactively
  validated) — before building heavily on AGE.

**Result (2026-06-12) — STAY single-engine (Postgres + AGE).** Harness:
`scripts/c3_age_density_benchmark.py`; full report committed at
`docs/trials/c3_age_density_benchmark.md`. Run on the ephemeral integration DB in an
isolated bench graph (`iknos_c3_bench`) whose indexes **mirror migration 0007's exact
DDL** (imported from the migration, retargeted — no drift), dropped on teardown; the real
`cypher()` seam is pointed at it so every query and `EXPLAIN` goes through the production
call path. Each read shape gets two plans — the planner's default and one with
`enable_seqscan=off` — so *index existence* is separated from *index use* precisely.

- **Index use confirmed (existence ≠ use).** At 30 000 vertices the planner **chooses**
  the migration-0007 vertex GIN for box-scoped retrieval (0.8 ms), MERGE-by-id
  (0.9 ms), and the bitemporal as-of (0.9 ms); the partonomy closure rides the **Actor GIN**
  (its anchor lookup). The agtype `properties @>` containment the 0007 docstring predicts is
  exactly what the plans use. No "index exists but unusable" case among the core reads — the
  §13 engine-swap trigger did **not** fire. *(`partOf` endpoint-btree use is **not demonstrated**
  — the shape-3 EXPLAIN shows only the Actor GIN; AGE's VLE may materialize edges internally.
  Anchor GIN verified; endpoint-btree use unshown. **Re-confirmed on the 2026-06-13 re-run** with
  the tightened `_scan_line`: shape 3 emits only `ix_actor_props` in both the default and
  `enable_seqscan=off` plans — no `partOf`-table or endpoint-btree scan node appears.)*
- **Costliest indexed read:** the `*1..5` `partOf` closure at ~52 ms median — AGE
  variable-length-traversal overhead, not a missing index. Acceptable at investigation
  scale; watch if the roll-up becomes a hot path. (The synthetic partonomy is a near-linear
  chain, fan-out ≈1, so this is a depth-bounded linear walk, not a branching roll-up.)
- **The one gap (W9 edge-property amendment):** `MATCH ()-[r:SAME_AS {state:…}]->()` has
  **no accelerating index** (the endpoint btree is a seq-scan substitute, not a `state`
  filter — edge-property GIN is deferred per the 0007 docstring). Concrete cost: the bulk
  bitemporal **supersession update** runs ~10²–10³× the indexed lookups (it rewrites every
  matching edge with a seq scan over `SAME_AS`). *(Re-measured 2026-06-13: **272 ms median /
  300 ms p95** at 30 000 vertices. The originally quoted ~1.3 s median / ~2.1 s p95 was
  **contaminated** — the old harness piled dead tuples by re-running the UPDATE on the same edges
  across all reps in one transaction, inflating it ~5×; the fixed harness isolates each write rep
  (per-rep rollback to the committed baseline). The STAY decision is unaffected — the defect had
  inflated an already-deferred cost.)* Bounded today by the small
  SAME_AS edge count, and real re-scoring touches few edges at a time. **Phase-5 action
  item:** add an edge-property GIN on `SAME_AS.properties` (or a btree on extracted `state`)
  before bitemporal supersession runs at reference-base scale.

---

## Instrument D — Multi-domain onboarding (graduation test)

Validates the central multi-domain claim. Cannot be judged from one domain.

### Trial D1 — Two contrasting domains: coverage, accuracy, code-vs-config  ⚠ may force redesign

- [ ] Onboard two deliberately different domains (e.g., equipment/ISO 14224, and a
      medical or financial corpus) as domain packs (§9).
- [ ] **Measure:** anchoring coverage and level-attachment accuracy in each (reusing A4
      metrics); and — the real test — how much **domain-specific code** vs pure
      pack-config each required.
- [ ] **Decision:** the system is "multi-domain" only if a new domain is added by
      authoring a pack + config with **no epistemic-layer code changes**.
- [ ] **Redesign trigger:** if a new domain needs code, the epistemic/domain split is
      leaking and must be tightened before scaling to more domains.
- **Gates:** any claim of multi-domain support; precedes onboarding further domains.

---

## Instrument E — Justification: baselines, ablation, ecological validity

Does the system *work on real evidence*, and is its complexity *justified*? The synthetic
A-series proves mechanisms, not efficacy; this instrument tests both efficacy and worth.
**E1 is an early go/no-go — run it before hardening and before Phases 5–7.**

### Trial E1 — Beat the cheap baseline (early go/no-go)  ⚠ may force descope/redesign

- [ ] Build a thin end-to-end slice (extract → link → adjudicate → answer) and the
      baseline ladder: plain RAG → agentic / multi-hop RAG → expert + good search, over
      the *same* corpus.
- [ ] **Scope the baselines as real work (2026-06 review, P5):** the go/no-go is only
      as valid as the strongest baseline — a weak RAG rig biases E1 toward the system.
      Budget the baseline implementations explicitly (retrieval-tuned plain RAG;
      multi-hop/agentic RAG with tool use; an expert+search protocol), start them
      before Phase 4 completes, and reuse them as Phase 1's retrieval sanity check.
      *(2026-06-11 review, F1: "before Phase 4 completes" has passed with zero
      baseline code. Execute the V4–V6 work breakdown below — one output contract
      so the V3 harness scores the whole ladder identically.)*

#### E1 work breakdown — V4/V5/V6 + V12 *(merged from `archive/gap_review_2026-06-11.md`; V12 from `archive/review_2026-06-12_completed_scope_residuals.md`)*

**V4 — baseline 1: tuned plain-RAG rig.** A *fair strong* baseline, not a
strawman: same LLM endpoint and embedding model as the system, but what a
competent team would build *without* this project. New `src/iknos/baselines/rag.py`:
fixed-size chunking (512 tokens / 64 overlap — deliberately **not** iknos
segmentation), own `baseline_chunks` table (own migration: id, document_id, text,
embedding vector(1024), model; embed through the existing substrate seam),
top-k cosine retrieval (k=8, configurable) + one answer call with citations via
`core/llm.py` (plumbing yes, project *reasoning* no — enforce with an import test
against segmentation/proposition/graph modules). **Shared output contract** for
all three rungs: `BaselineAnswer {question_id, answer_text, cited_chunk_ids,
confidence}` — confidence is the model's verbalized 0–1 (the baseline's own
calibration story; do not multi-sample it). Runner `scripts/run_baseline.py
--baseline rag --corpus … --questions <toml>` → `docs/trials/
baseline_rag_answers.toml` (add `questions.toml` to the gate corpus if V1 did
not). Tests: chunk boundaries + prompt assembly (mock LLM); small
ingest+retrieve integration. Tuning knobs are constructor params. No reranker/
query rewriting (that is V5); no scoring (V3's job).

**V5 — baseline 2: agentic / multi-hop RAG rig** *(after V4)*. The strongest
cheap competitor: an LLM-driven loop (max 6 steps) over `search(query)` (V4's
retrieval) and `answer(text, citations, confidence)` — query reformulation and
multiple searches allowed, must end with `answer`. Implement directly on
`core/llm.py` structured output (no agent-framework dependency). Same output
contract + `--baseline agentic`. Persist the per-question **trace** (queries
issued, chunks seen) — E1's traceability axis scores what the baseline can cite,
so the trace must be complete. Malformed tool call → one retry then record the
question as unanswered loudly. Budget: ≤ 6 LLM calls + 1 answer per question.
Never give it iknos's graph/propositions/contradiction machinery.

**V6 — baseline 3: expert+search protocol** *(no code — do not let it block the
others)*. `docs/trials/e1_expert_search_protocol.md`: who (the V2 second
annotator or another colleague — **not** the developer, who knows the answers);
toolset (corpus as plain files + editor/ripgrep search, no iknos); time box
(~25 min/question); record per question: answer, relied-on passages, 0–1
confidence, time. Plus `docs/trials/e1_expert_answers_template.toml` matching
the V4/V5 contract. Contamination rule: the expert has not read
`gate_corpus/README.md` or the labels.

**V12 — baseline-rig hardening (V4/V5 residuals)** *(2026-06-12 residual review,
findings 1/2/5/6/11 — `archive/review_2026-06-12_completed_scope_residuals.md`;
land before any E1 measurement run — findings 1 and 2 bias the instrument itself)*.
One PR: (a) **retrieval scoping + stale chunks**: `rag.py::ingest_document` upserts
on `(document_id, chunk_index, model)` but never deletes rows beyond the new chunk
count — re-ingesting a shortened document leaves stale tail chunks, contradicting
its own "re-run after a corpus edit is safe" docstring (`rag.py:203`); and
`retrieve()` filters only on `model` (`rag.py:251`), so chunks from any previously
ingested corpus contaminate retrieval and get cited. Delete stale tails on
re-ingest, and scope retrieval to the run's corpus (a corpus/run identifier on
`baseline_chunks` — needs a migration; set `down_revision` from `alembic heads` at
PR time — or an explicit document-id set threaded from the runner; pick the one
that keeps the rig honest for the two-corpus case and record why). (b) **pinned
sampling**: baselines store `sampling=None` (`rag.py:187`, `agentic.py:157`) where
every other consumer pins greedy (`{"temperature": 0.0}` — `core/extract.py:288`
et al.); pin the same default, expose it as a constructor/CLI knob, and **record
the sampling regime in the answers-file `meta`** (`run_baseline.py:108-116`) so a
score is reproducible and attributable as `contract.py` claims. (c) **budget in
calls, not steps**: `_step_call` retries don't consume the step budget
(`agentic.py:160-181`) — worst case 13 LLM calls vs the spec's "≤ 6 LLM calls +
1 answer"; count decision *calls* against the budget so the bound holds. (Note:
under the now-pinned greedy regime a byte-identical retry deterministically
reproduces the malformed output — vary the retry, e.g. a corrective system note,
or drop straight to loud-unanswered; decide and document.) (d) **import-boundary
relative-import bypass**: both AST guards keep only `node.level == 0` imports
(`tests/unit/test_baselines_import_boundary.py:36`,
`tests/unit/test_trials_import_boundary.py:30`), so `from ..core import …` escapes
the boundary undetected — resolve relative imports against the package path and
run them through the same allow/deny lists. (e) nits, if cheap in passing:
`contract.py:124-133` `_toml_str` escapes only `\` `"` `\n` `\t` `\r` — escape the
remaining control characters (U+0000–U+001F, U+007F) so an LLM answer containing a
form feed cannot break V3's `tomllib` parse; `run_baseline.py:103` logs
"answered %s" for unanswered questions. **Do not** change the BaselineAnswer
contract fields, the chunking parameters, or k — V3 comparability and the
already-written V6 template depend on them.
- [ ] **Measure on the differentiator axes** (where RAG is weak — an easy factoid tie is
      fine): contradiction / refuter handling; correct **retraction** when an overturning
      fact is added; completeness/correctness of **traceability** to source; **calibration**
      of confidence; and reviews-to-correct-conclusion (ties to A7). Bias-controlled
      (gold answers, controlled ordering — not LLM-as-judge).
- [ ] **Decision (go/no-go):** proceed to harden + Phases 5–7 only if the system shows
      **material lift over the strongest cheap baseline on the differentiator axes**.
- [ ] **Descope/redesign trigger:** if it does not, stop and rethink — keep only the
      components the ablation (E2) shows carry the value. This is the single most
      important check; failing it after a full build is the expensive failure mode.

### Trial E2 — Component ablation (find the value-carrying fraction)

- [ ] Ablate one component at a time and measure value lost: no part-whole hierarchy
      (flat); no two-layer propagation (no foundedness/retraction); no QBAF (sum
      evidence); no candidate funnel; no ensemble contradiction gate (single LLM); no
      entity resolution beyond exact-match; no multi-sample/calibration (raw LLM).
- [ ] **Measure:** per-component contribution on the E1 differentiator axes.
- [ ] **Decision:** identify the load-bearing components vs the marginal ones; if
      descoping is ever needed, this is the data-driven minimal viable system — not a
      guess.
- [ ] **De-scoping ladder (W10 — 2026-06-11 architecture assessment).** Name the
      candidate minimal configurations *in advance*, so a mixed E1 result has a
      pre-agreed landing zone instead of an unstructured rethink. The ablation arms
      double as the rungs; the named ladder, smallest first:
      1. **Perception only** — parse → propositions + epistemic fields +
         faithfulness + provenance + hybrid retrieval (no graph reasoning): a
         traceable, calibrated evidence-retrieval product.
      2. **+ graph & resolution** — entities, `SAME_AS`, evidential edges, manual
         adjudication: an evidence-mapping product, expert reasons over the graph.
      3. **+ Layer A/B** — foundedness + confidence propagation, no QBAF: retraction
         works, verdicts are manual.
      4. **Full system** — QBAF + ensemble gate + VoI triage.
      E2 measures which rung the value plateaus at; the rung *is* the descope
      decision. Record the chosen rung and rationale in `todo.md` if ever invoked.

### Trial E3 — Ecological validity (retrospective real case)

- [ ] Run the system on the raw, messy documents of a **real, already-resolved
      investigation** (a closed RCA / incident / post-mortem with a known outcome).
- [ ] Inject realistic messiness into the synthetic corpus too (hedged language, OCR
      noise, near-duplicate + irrelevant documents, partial information) to bridge
      synthetic → real.
- [ ] **Measure:** does it reach the known conclusion and surface the real contradictions
      on messy evidence?
- [ ] **The validity ladder is explicit:** synthetic (mechanisms) → retrospective real
      (ecological, known answer) → prospective/live (expert uses it on an open case).
      **Never claim efficacy from the synthetic gate alone.**
- **Gates:** any efficacy claim; prospective/live use is the top rung, available only
  after a working system.

---

## Gate prerequisites — infrastructure *(merged from `archive/gap_review_2026-06.md` R10/R11; land before the gate trials ingest the V1 corpus — a real multi-document ingest as a synchronous in-process foreground job is the failure mode these prevent)*

**R10 — serve embedding inference out-of-process.** `EmbeddingSubstrate` loads
bge-m3 into the calling process. Put it behind the same swappable-service seam as
the LLM and parser (copy `core/mineru.py` httpx/pydantic/retry pattern +
`core/parse.py` protocol/factory/fallback pattern): (1) `EmbeddingBackend`
protocol in `core/embeddings.py` (`embed_document`, `embed_passages`) — the
current in-process class is the default/local backend; (2) new
`core/embeddings_http.py::HTTPEmbeddingBackend` — TEI-compatible for
`embed_passages`, custom `/embed_document` endpoint for the windowed token+offsets
path (our versioned wire schema: `{text, window_tokens, overlap_tokens}` →
`{model_version, offsets, embeddings}`; pydantic-validated, reject length
mismatches; retries transport/5xx only; `EMBEDDINGS_TIMEOUT_S` default 300);
(3) `make_embedding_backend()` factory keyed on `EMBEDDINGS_BASE_URL` (empty ⇒
local — the `parser_base_url` pattern); (4) the server itself is ops-side (stub
README in `local-llm-setup/`). Accept: unset URL → byte-identical behavior, all
existing tests untouched; HTTP backend wire-validated; `DocumentContext`
interchangeable on a fixture. Tests: `test_embeddings_http.py` with httpx
MockTransport, mirroring `test_mineru.py`. Do not deprecate the in-process
backend or change `DocumentContext`.

**R11 — background job queue (procrastinate).** Postgres-native (LISTEN/NOTIFY
on the existing engine — no new infra; principle 7). Realizes the §6 concurrency
contract: one ingest worker per document; one investigation's graph writes
serialize through one queue. (1) add `procrastinate[psycopg]`; (2)
`src/iknos/jobs/app.py`: app bound to `DATABASE_URL`; task
`ingest_document_bytes_job(document_id, content_b64|storage_ref, title, box)`
wrapping `core/ingest.ingest_document_bytes` in a session/transaction; retry max
3, exponential backoff, transport-class errors only — validation errors
(`DocumentTooLongError`, `DocumentResegmentationError`, parse validation) are
terminal; (3) queue `ingest:<box_id>` with per-queue concurrency 1 +
`queueing_lock` on document id (no two jobs for one document concurrently);
(4) procrastinate schema via a migration embedding its DDL, or a documented
one-shot `uv run procrastinate schema --apply` in `MIGRATIONS.md` — choose one,
write it down; (5) `api/main.py`: `POST /documents` (multipart, enqueues, returns
job id) + `GET /jobs/{id}`; no auth yet (Phase 6 entry criterion); (6)
`compose.yaml`: a `worker` service (same image, `uv run procrastinate worker`) —
**compose changes are reviewed, not run** (host policy: no `docker compose up`
without approval). Accept: enqueue→worker→committed spans + Actions + `succeeded`;
validation error → `failed`, no retry; transport error → ≤3 retries; same-document
lock holds; direct synchronous callers unchanged. Tests: pure
retry-classification unit test; enqueue→run integration via
`procrastinate.testing.InMemoryConnector` (no live worker container).

**R10/R11 follow-through (2026-06-12 — the gate dry-run report flagged "the job covers
ingest only").** Two resolutions, both in `src/iknos/jobs/app.py` + `core/`:

- **R10 seam threaded through ingest.** `core/ingest` (and `Propositionizer`) now take the
  `EmbeddingBackend` protocol, and the R11 worker constructs the backend via
  `make_embedding_backend()` — so `EMBEDDINGS_BASE_URL` set ⇒ the worker holds no torch, unset ⇒
  byte-identical in-process bge-m3. (Also fixed a latent worker bug: the worker built its own
  engine without the AGE connect-bootstrap, so `cypher()` would have failed on the first `Span`
  MERGE — the dry run would have hit it. Factored `db/session.register_age_bootstrap` and applied
  it; connection-level, so it survives the per-item rollbacks ingest/extract use.)
- **Queue scope: perception and extraction are two tasks, not one job.** `ingest_document_bytes_job`
  runs perception (parse → segment → embed → persist `Span`s) and, **on success**, chains
  `propositionize_document_job` (the LLM extraction pass → `Proposition`s + faithfulness). They
  have different cost/retry profiles — folding them into one job would re-embed on every LLM
  transport blip — so they are separate tasks sharing the box's queue + execution lock (serialized,
  no graph-write race) with their own `queueing_lock`s; both content-hash idempotent (a re-fired
  chain is a no-op). The follow-on reloads spans by document id via `core.ingest.load_document_spans`
  (level 0, the extraction granularity). Re-*inference* gating stays the separate §6.1/§11.1 VoI/
  budget policy (Phase 5); this chains only the *initial* extraction of freshly-ingested spans.

## Sequencing & gating summary

```
A0 (build corpus + harness — START NOW, parallel with Phase 1 tail)
   ├─► A5 (extraction faithfulness) ┐
   ├─► A6 (entity resolution)       ├─ foundation gates ─► Phase 1/2; precede A1–A4
   └─► A1, A2, A3, A4   ── gate ──►  harden Phases 3 & 4   (assume faithful atoms + sound identity)
C3 (AGE density bench)   ── gate ──►  Phase 2 ENTRY (with G0.R2 indexes) — pulled forward
E1 (beat cheap baseline) ══ GO/NO-GO ══►  before hardening & Phases 5–7 (E2 ablation guides descope;
                                      baselines scoped as real work, started before Phase 4 ends)
B1, B2 (on thin slice)   ── gate ──►  build Phase 6 frontier / Phase 7 controls
                                      (B2 also gates Phase 4 oscillation handling)
C1                       ── gate ──►  Phase 5 triggers / Phase 3 Layer A↔B interface
C2                       ── defer ──►  scale path only (revisit, not MVP-blocking)
D1 (two domains)         ── gate ──►  claiming multi-domain; onboarding more domains
E3 (retrospective real)  ── gate ──►  any efficacy claim (validity ladder)
```

**Ordering deltas from the 2026-06 review:** A0 starts immediately (it gates
everything and depends on nothing unbuilt); C3 moves from "scale trial" to Phase 2
entry; G1.14 (polarity-aware clustering) lands **before** A5 fits
`PROP_AGREEMENT_THRESHOLD`, or the calibration bakes the polarity-blind bug in;
A5's combiner check is now **falsification, not a choice** — the default is decided
(§3.1: `verify × calibrate(agreement)`, `min` rejected); A5 verifies the decision
empirically and lands after G1.20 (`calibrate` seam) so thresholds fit the final code
path.

**Run earliest (can fail and force redesign/descope):** **E1 (beat the cheap baseline —
the go/no-go on the whole approach)**, A5 (extraction faithfulness) and A6 (entity
resolution — the foundation), then A1 (refuter recall), A4 (anchoring coverage / level
accuracy), B1 (significance-weighted frontier), D1 (code-vs-config portability).
Everything else is tune-to-fit or measure-and-pick.

**One instrument, many answers:** A0's corpus + harness resolves A1–A7 (A7 via a
simulated oracle reviewer) and feeds B2 — build it once, well; it is the single
highest-leverage asset in the plan. A5 and A6 are the *first* gates to clear: faithful
atoms and sound identity are the precondition for every reasoning trial (A4's
anchoring/level accuracy is itself bounded by A6).
