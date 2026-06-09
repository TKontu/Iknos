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

### Trial A0 — Build the planted-corpus and evaluation harness *(shared prerequisite)*

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

### Trial C3 — Storage-engine viability under schema density  ⚠ may force engine change

- [ ] Benchmark **Apache AGE** under the *real* schema density — every node/edge carrying
      provenance, two annotations, sensitivity, conditional credibility, bitemporal
      validity, overrides — at investigation scale (tens of docs) + a static reference
      base, on the actual query patterns (box-scoped retrieval, `WITH RECURSIVE` closure,
      SCC detection, bitemporal as-of).
- [ ] **Scope bitemporality:** confirm it is applied where needed (boxes, overrides,
      packs) and not bloating every `SAME_AS` edge.
- [ ] **Decision:** stay single-engine (Postgres + AGE) if it meets latency; the fallback
      is a separate graph store at the cost of single-engine simplicity (§6, §13).
- **Gates:** Phase 0 — schema/engine commitment before building heavily on AGE.

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

## Sequencing & gating summary

```
A0 (build corpus + harness)
   ├─► A5 (extraction faithfulness) ┐
   ├─► A6 (entity resolution)       ├─ foundation gates ─► Phase 1/2; precede A1–A4
   └─► A1, A2, A3, A4   ── gate ──►  harden Phases 3 & 4   (assume faithful atoms + sound identity)
E1 (beat cheap baseline) ══ GO/NO-GO ══►  before hardening & Phases 5–7 (E2 ablation guides descope)
B1, B2 (on thin slice)   ── gate ──►  build Phase 6 frontier / Phase 7 controls
                                      (B2 also gates Phase 4 oscillation handling)
C1                       ── gate ──►  Phase 5 triggers / Phase 3 Layer A↔B interface
C2                       ── defer ──►  scale path only (revisit, not MVP-blocking)
D1 (two domains)         ── gate ──►  claiming multi-domain; onboarding more domains
E3 (retrospective real)  ── gate ──►  any efficacy claim (validity ladder)
```

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
