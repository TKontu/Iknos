# Phase 4 — Evidence Linking & Adjudication

**Goal:** connect evidence to hypotheses with well-judged edges and adjudicate
hypothesis state. Contains the hardest judgment (connection strength) and the most
bias-prone step, so it is heavily disciplined. Closes with the **validation gate**.

**Depends on:** Phase 2 (nodes), Phase 3 (Layer B confidence). Built in parallel with
Phase 3 as a thin slice.
**Architecture refs:** §5 (edge model), §5.1 (candidate generation), §8 (edge
disciplines, confidence pipeline, experiment), §7.2 (ensemble gate, hypothesis state),
§10 (`sign`/`strength`/`significance`).

**Status — 🟡 adjudication core + persistence landed (G4.1, G4.4); candidate-generation funnel
complete across both cheap stages (G4.2 slice 1 structural + slice 2 embedding k-NN); the
edge-judgment pipeline now runs end-to-end (G4.3 slice 1 subjective-logic algebra + slice 2
blind/randomized judge + slice 3 AGE producer that persists the judged `SUPPORTS`/`REFUTES`
edges) — funnel → judge → calibrated edge → QBAF is a closed loop; the **§7.2 ensemble gate's
refuted-flip authoriser landed (G4.5 slice 1, `core/ensemble_gate.py`)** — the pure decision
algebra over the LLM/symbolic/temporal channels that authorises a persisted `refuted` flip
(unanimity-of-required + universal dissent veto, `DEFAULT_GATE` decided by a fixture);
`corroborate`/`find-contradiction` operators + the gate's channel producers/consumer-filter
(rest of G4.5) and the validation gate (G4.6) open.**
G4.1 (`core/qbaf.py`) ships the pure QBAF gradual-semantics engine:
the **semantics decision** (DF-QuAD vs Quadratic Energy, decided with a fixture — DF-QuAD the
conservative default, both retained at the seam), the `solve` bounded fixpoint (acyclic-exact,
cyclic non-convergence **surfaced as a finding** not smoothed, §13), and the read-off
(acceptability → §11.2 verdict band + computed hypothesis state). It consumes Layer B
confidence as the base score (§12 seam). **G4.4** (`core/qbaf_adapter.py`) wires it to real AGE:
loads the active `SUPPORTS`/`REFUTES` subgraph + base scores → `BAF`, adjudicates, and writes the
computed `acceptability`/`state` back to the `Hypothesis` node (partial `SET`, band derived-not-
stored); it reuses the shared `load_active_box_ids`/`load_reasoning_nodes` reads and consumes the
`types/intentional.py` vocabulary (the G4.1 banding/state duplication was reconciled here).
**G4.3 slice 1** (`core/subjective_logic.py`) lands the pure subjective-logic confidence-scoring
core (§8(c), steps 3–4): the binomial `Opinion`, the multi-sample-consistency → opinion map,
source-reliability discounting, and **cumulative/averaging fusion** — with the fusion **decided
by a fixture** (`DEFAULT_FUSION = AVERAGING`, idempotent under correlated evidence so it cannot
inflate certainty; cumulative retained at the seam). The fused/discounted opinion's projected
probability *is* the calibrated edge `strength` the QBAF consumes. **G4.3 slice 2**
(`core/edge_judge.py`) lands the **blind, randomized, multi-sample LLM edge judge** (§8): per
hypothesis it judges the whole candidate set **together** (relative, not pair-by-pair),
**blind** to the hypothesis state (sycophancy guard), with a **per-sample permutation** of the
evidence (position-bias guard, content-addressed so it is replayable and the diversity source at
temperature 0); it classifies **sign only** (supports/refutes/irrelevant — no verbalized
magnitude), drops `irrelevant`-plurality pairs, and folds the per-sample votes into the
G4.3-slice-1 `opinion_from_evidence` → `discount` → projected-probability read-off — the
calibrated `strength`, with a `sign_stable` finding when the panel splits direction (§13). It is
the DB-free LLM layer between the funnel and the read-off. **G4.3 slice 3** (`core/edge_producer.py`)
closes the pipeline: the **AGE producer** reads the G4.2 candidate pool, resolves each node's
`statement` + each evidence node's `effective_credibility`, runs the slice-2 judge concurrently, and
writes each surviving `SUPPORTS`/`REFUTES` edge (calibrated `strength`, derived `significance`,
`sign_stable` finding) plus a provenance `Action` (§10.1) in one transaction — keeping
strength/significance/credibility the **three separate quantities** §3.1/§8/§9 mandate (strength the
pure connection judgment; `effective_credibility` routed into `significance` per §9, not the
strength discount). **G4.2 slice 1**
(`core/candidates.py`) lands the candidate-generation funnel (§5.1): the recall-first **funnel
core** (`funnel` + `CandidatePool`, with the union-over-intersect combination **decided by a
fixture** — `DEFAULT_STRATEGY = UNION`, so the dissimilar refuter the embedding stage misses
survives; intersect retained at the seam) + the **structural-entity prior** (stage 1: shared
`INVOLVES` `Actor`/`Object`, active-box-scoped, evidence → hypothesis), separate from the §8
judgment that consumes the survivors. **G4.2 slice 2** adds the **embedding k-NN workhorse** (stage
2: `embedding_knn_candidates` — each node traced `EVIDENCED_BY` → `Proposition` → its
`proposition_embeddings` vector, the `k` nearest claims by cosine union in as `EMBEDDING_KNN`, with
the recall-first **no-similarity-floor decision** mirroring the funnel's `UNION` and the G1.16
model-identity guard enforced). Coarse-to-fine (stage 3) + keyword co-occurrence remain documented
seams. The remaining edge-judgment refinement (§8, G4.3 — per-model recalibration, identity until
G4.6) and the tier-differentiated significance weighting are open seams; the `corroborate` /
`find-contradiction` operators + ensemble gate (§7.2, G4.5), and the validation gate (§8, G4.6)
are the next increments. Full per-slice decision records: `docs/archive/gap_phase_4_linking_adjudication.md`.

**Sequencing override (2026-06-11 review, F1/F2).** Before the remaining G4.5
slices (channel producers, operators): the **safety lockdown must land** —
**R8 → R9 → V7 (quarantine enforcement in the edge producer) shipped** (the
`provisional`→`provisional_reasons` set, the pure `core/quarantine` gate, and
the edge-producer drop-and-record); **V8** (the `persist_verdicts` ensemble
filter, i.e. the "consumer-filter" slice of G4.5, wired to the slice-1
`authorise` and holding un-authorised flips as a `pending_refutation` finding)
is the remaining lockdown slice — `persist_verdicts` still writes whatever
state it is given. And before G4.6 can run at all: the **gate assets** V1 (planted
corpus), V2 (gold labels — longest lead, start the annotator recruitment now),
V3 (metrics harness), plus the E1 baselines V4–V6 — specs in `todo_trials.md`.
The lockdown specs are in *Open task specs* below.

## Candidate generation (§5.1) — which pairs to assess

- [x] Funnel, cheap → expensive; **two stages separate from adjudication**. *(G4.2 slice 1 —
      `core/candidates.py`: `funnel(*generators, strategy)` combines the cheap stages into a
      deduped `CandidatePool`, separate from the §8 judgment which consumes the survivors.)*
- [~] Structural priors: shared `Actor`/`Object` (`INVOLVES`), sparse/keyword
      co-occurrence; box/tier-scoped. Near-free, filters the bulk. *(G4.2 slice 1 —
      `structural_entity_candidates` ships the shared-`INVOLVES`-entity prior (stage 1),
      active-box-scoped via the shared reads. **Open:** sparse/keyword co-occurrence — a further
      `STRUCTURAL_KEYWORD` `CandidateSource` (`PropositionLexicalIndex`) that unions at the seam.)*
- [x] Embedding **k-NN** over pgvector — the workhorse. *(G4.2 slice 2 — `embedding_knn_candidates`
      + the `CandidateGenerationAdapter` cross-store read: each active reasoning node is traced
      `EVIDENCED_BY` → `Proposition` → its `proposition_embeddings` vector, and the **`k` nearest
      claims by cosine** become `EMBEDDING_KNN` candidates per hypothesis. **Exact** in-memory
      cosine over the active working set (the recall ceiling the §8 gate measures any ANN index
      against); the model column is the G1.16 vector-space identity guard (no cross-model cosine).
      The pgvector `<=>` ivfflat/hnsw push-down is the documented performance seam.)*
- [ ] **Coarse-to-fine** over the §2 abstraction levels: match coarse, descend to
      proposition pairs only within survivors. *(G4.2 slice-2 seam: needs the `partOf` level
      derivation, §14.)*
- [x] **Tune for recall early, precision late** — a missed candidate is a silent
      false negative. *(G4.2 slice 1 — candidates are **unscored** at this layer and the funnel
      **unions** generators (`DEFAULT_STRATEGY = UNION`, decided by a fixture); precision is the
      §8 LLM stage's job.)*
- [~] **Dissimilar-refuter handling:** hypotheses pull candidates by constituent
      entities + topic, not similarity alone; `find-contradiction` is a first-class
      generator, not a similarity by-product. *(G4.2 slice 1 — the structural prior pulls by
      constituent entity (not embedding) and the union default keeps the dissimilar refuter the
      embedding stage misses — the decision fixture. **Open:** `find-contradiction` as a
      dedicated refuter generator is G4.5.)*

## Edge adjudication (§8 disciplines) — the bias-hardened judgment

- [x] **Sign before magnitude:** classify direction (supports/refutes/irrelevant)
      first; estimate strength only for non-irrelevant edges. *(G4.3 slice 2 —
      `core/edge_judge.py`: the judge emits a categorical `JudgedSign`
      (supports/refutes/irrelevant) and **no number** (the schema has no magnitude field);
      an `irrelevant` plurality drops the pair, strength is estimated only for the
      non-irrelevant survivors and the directional sign becomes the `SUPPORTS`/`REFUTES`
      edge type.)*
- [~] **Relative, not absolute:** elicit strength by ranking/pairwise comparison of
      competing evidence on the same hypothesis. *(G4.3 slice 2 — a hypothesis's whole
      candidate set is judged **together** in one prompt (the competing evidence weighed
      relative to each other), not pair-by-pair; magnitude is **never elicited** as a number
      — it emerges from cross-sample consistency. **Open:** an explicit ranking/pairwise
      elicitation over the set is a further refinement at the same seam.)*
- [x] **Blind + randomized:** judge blind to current hypothesis state (sycophancy
      guard); randomize evidence order across samples (position-bias guard). *(G4.3 slice 2 —
      the prompt carries the hypothesis + evidence and **nothing** about the hypothesis's
      acceptability/state (blind); each sample sees a **per-sample permutation** of the
      evidence (`_permutation`), content-addressed on `(hypothesis_id, sample_index)` so a run
      is replayable yet position-bias-probing — and the source of sample diversity even at
      temperature 0.)*
- [~] **Multi-sample consistency**, per-model recalibration, encode as subjective-logic
      opinion with source discounting, fuse with cumulative/averaging (not raw
      Dempster's rule). *(G4.3 slice 1 — `core/subjective_logic.py`: the pure algebra —
      `Opinion`, `opinion_from_evidence` (the consistency→opinion map), `discount` (source
      reliability, the §8↔§9.1 seam), and `cumulative_fuse`/`averaging_fuse`/`fuse` with
      `DEFAULT_FUSION = AVERAGING` decided by a fixture (idempotent under correlated evidence —
      cannot inflate; cumulative retained at the seam). **G4.3 slice 2** — `core/edge_judge.py`
      runs the blind/randomized panel and tallies the per-sample votes into the
      `(positive, negative)` counts `opinion_from_evidence` consumes (`irrelevant` votes
      abstain → raise uncertainty), discounts by source reliability, and reads off the
      projected probability as the calibrated edge `strength`; sign instability (both
      directions voted) is surfaced as a `sign_stable=False` finding (§13), the signal the
      G4.5 ensemble gate consumes. **Open:** per-model recalibration (a fitted curve, identity
      until G4.6) and cross-judge fusion (the ensemble, G4.5).)*
- [x] Write `SUPPORTS`/`REFUTES` edges carrying `sign`, fused/recalibrated `strength`,
      and `significance` (from the node/tier). Stored `strength` is **never** the raw
      LLM number (§10). *(G4.3 slice 3 — `core/edge_producer.py`: the AGE producer reads the
      G4.2 candidate pool, resolves each node's `statement` + each evidence node's
      `effective_credibility`, runs the slice-2 judge, and writes each surviving edge
      (`merge_edge` `SUPPORTS`/`REFUTES`) with the **calibrated** `strength` (the multi-sample
      opinion's projected probability), a derived `significance`, the `sign_stable` finding, and a
      provenance `Action` (raw votes + sampling + `prompt_sha`/`schema_sha`/`schema_version`,
      §10.1). **Reconciles the §8/§9 credibility routing:** strength stays the *pure connection
      judgment* (judge fed identity reliability) and `effective_credibility` is routed into
      `significance` (§9), keeping strength/significance/credibility the "three separate quantities,
      never merged" (§3.1/§8). The QBAF adapter (G4.4) consumes exactly these edges. **Open:**
      per-model recalibration (the fitted consistency→correctness curve, identity until G4.6) and
      tier-differentiated significance (the `SignificancePolicy` is uniform until G4.6 calibrates
      it).)*
- [x] **Quarantine enforcement at the edge-creation site (V7, needs R8+R9):** a
      provisional-sourced `REFUTES` (or sole-support `SUPPORTS`) is dropped from the
      plan and recorded on the `Action` as `quarantined` — never persisted, never a
      silent skip (§3.1). The judge still sees the evidence; quarantine gates the
      *write*. *(Shipped — R8 `provisional_reasons` set + R9 `core/quarantine`
      (`Stakes`, `assert_not_quarantined`) + V7 `edge_producer` drop-and-record
      (`edge_stakes`, `_load_provisional_reasons`, `QuarantineRecord`,
      `outputs.quarantined`); `qbaf_adapter` untouched, a quarantined edge is simply
      never written. Spec in *Open task specs* below.)*
- [ ] `corroborate` operator: hypothesis → gather supporting/refuting evidence.
- [~] `find-contradiction` operator + **ensemble gate** (multi-sample LLM + symbolic +
      temporal agreement) required before any `REFUTES` (§7.2). *(G4.5 slice 1 —
      `core/ensemble_gate.py`: the **refuted-flip authoriser**, the pure decision algebra over the
      three channels. `authorise(signals, gate)` authorises a persisted `refuted` flip **iff every
      required channel AFFIRMs and no channel DISSENTs** (a dissent vetoes under every policy — a
      §13 finding, never out-voted); the gate policy is a **value decided by a fixture**
      (`DEFAULT_GATE` = `{LLM, SYMBOLIC}` required, `TEMPORAL` conditional — the conservative,
      cannot-inflate choice; `STRICT_GATE`/`LLM_ONLY_GATE` retained at the seam). `llm_channel`
      bridges the G4.3 panel (stable `REFUTES` → AFFIRM; sign-unstable / no-refuter → ABSTAIN — the
      `sign_stable=False` finding the gate "must clear"). A withheld flip is `is_finding` — surfaced
      for expert review, not auto-persisted; with `SYMBOLIC` required but unwired the default gate is
      **safe-by-default** (no automated flip until the producer lands). **Open (later G4.5 slices):**
      the symbolic (clingo/ASP) + temporal (bitemporal) channel **producers** — ABSTAIN seams today —
      the `persist_verdicts` **filter** that drops un-authorised flips, and `corroborate` /
      `find-contradiction` feeding the `REFUTES→retract→A→B→QBAF` body into the G3.9 `stabilize`
      driver.)*

## Adjudication (QBAF)

- [x] Model supports/refutes as a **Quantitative Bipolar Argumentation Framework**;
      Layer B confidence is the base score. *(G4.1 — `core/qbaf.py`: `BAF` (arguments +
      weighted `Edge` support/attack); `solve` consumes a `base` map = Layer B confidence as
      the intrinsic score (§12 seam), one edge contributing `strength·σ(src)`. **G4.4** —
      `core/qbaf_adapter.py` loads the real active `SUPPORTS`/`REFUTES` subgraph + base scores
      (the node `confidence`) from AGE into the `BAF`, edge direction fixed by the schema
      (Fact/Conclusion → Hypothesis); integration-tested on live AGE.)*
- [x] Gradual semantics (DF-QuAD or Quadratic Energy), in-house (QBAF-Py/Uncertainpy
      as reference only). *(G4.1 — both in-house as `GradualSemantics` values
      (`DF_QUAD`/`QUADRATIC_ENERGY`), the engine generic over one; **decided with a fixture:
      `DEFAULT_SEMANTICS = DF_QUAD`** (conservative under correlated error — saturates rather
      than accrues), Quadratic Energy retained at the seam. `tests/unit/test_qbaf_semantics.py`
      shows the two rank the same hypotheses oppositely.)*
- [~] **Hypothesis state machine:** compute supported/unsupported/refuted +
      `acceptability` from incoming evidence; state is computed, never hand-set (§10).
      *(G4.1 — `acceptability` computed by `solve`; `classify_state` derives
      supported/refuted/unsupported and `intentional.band` the §11.2 verdict, both **computed,
      never hand-set**. **G4.4** — `QbafAdapter.evaluate` runs this over real AGE and
      `persist_verdicts` writes `acceptability`/`state` back to the `Hypothesis` node (partial
      `SET`; band derived-not-stored). **G4.5 slice 1** — the flip *to* `refuted` is now authorised
      by the **ensemble gate** (`core/ensemble_gate.py`): `classify_state`'s structural `REFUTED` is
      the gate's *input*, not a licence; `authorise` clears it only on required-channel agreement
      with no dissent. **Open:** wiring the gate as the `persist_verdicts` **filter** (so un-authorised
      flips are surfaced, not written) is the rest of G4.5 — `persist_verdicts` still writes what
      it's given.)*
- [x] Bound iteration + detect oscillation on cyclic argument graphs; surface
      unresolved regions rather than forcing convergence (principle 8, §13). *(G4.1 — `solve`
      bounds the fixpoint iteration and, on hitting the bound, returns `converged=False` with
      the still-moving arguments in `QbafResult.unstable` (`is_finding`) — surfaced, never
      smoothed into a verdict. Period-true oscillation over *discrete* loop states is the outer
      `core/composed_loop.py::stabilize` driver, G3.9.)*

## Validation gate (§8 experiment) — run before hardening anything

> **Work breakdown (2026-06-11):** the gate's assets are granular agent-executable
> tasks — V1 (corpus), V2 (gold labels + annotators), V3 (metrics harness +
> bias-controlled scoring), V4–V6 (E1 baseline ladder) in `todo_trials.md`, and
> V9 (the ANN recall-vs-exact measurement, *Open task specs* below) folded into
> the gate run. The checkboxes below are satisfied by those tasks landing; do
> not duplicate work.

- [ ] Build the planted-contradiction corpus (sources with conflicts + a later
      overturning fact); keep as a regression suite.
- [ ] Measure: retraction propagation (Phase 3); hypothesis-state flip on the
      overturning fact; consistency vs verbalized confidence; ensemble vs single-call
      contradiction; **candidate recall, especially refuter recall**; **fact→referent
      level-attachment accuracy** (anchored vs induced) against human labels.
- [ ] **Bias-controlled evaluation:** score against domain gold answers with controlled
      answer ordering — **not** LLM-as-judge headline scores, which carry large
      position/length bias (§8, §13).
- [ ] **Accuracy gates before automation:** level attachment gated on inter-annotator
      agreement (κ > 0.6) before automating; inferred-level embeddings gated on
      depth-recovery correlation (ρ > 0.6) before they are trusted (§13/§14).
- [ ] **Gate:** do not proceed to Phase 5 / do not harden until results are acceptable.
      A failure changes the design.

## Exit criteria

- [ ] Evidence links to hypotheses with sign/strength/significance via the disciplined
      pipeline; the funnel makes assessment targeted, not all-pairs.
- [ ] Hypothesis state is computed by the QBAF layer and updates as evidence changes.
- [ ] The validation gate has been run and passed.

## Phase risks / decisions

- **LLM→QBAF weight mapping** is unstandardized — design and validate it here (§13).
- **Correlated LLM error** is not removed by the disciplines — use varied judges, flag
  suspiciously uniform strengths; record as a known limitation (§13).
- **Cyclic QBAF convergence** has no general guarantee — the requirement is
  detect/bound/surface, not converge (principle 8, §13).

## Open task specs *(merged from `archive/gap_review_2026-06.md` R4/R8/R9 and `archive/gap_review_2026-06-11.md` V7/V8/V9, 2026-06-11 — execute as written; one task per PR, branch `fix/<id>-<slug>`)*

Work-stream order: **R8 → R9 → V7 → V8** (the safety lockdown — before the remaining
G4.5 slices); **R8/R9/V7 are shipped**, **V8** is the remaining lockdown slice. And
**R4 → V9** (gate ANN infrastructure — with the gate trials).
Migrations: set `down_revision` to the actual head (`alembic heads`) — numbering in
older specs is stale.

### R8 — `provisional` boolean → `provisional_reasons` set *(shipped)*

One flag currently carries three meanings; triage (§11.1) needs the reason, the
quarantine gate (R9) needs non-emptiness. Known reasons now: `low_faithfulness`
(Phase 1), `unresolved_reference` (Phase 2), `uninferred_budget` (Phase 5).

1. `types/epistemic.py`: add `ProvisionalReason(StrEnum)` with those three values;
   replace `is_provisional(...)` with
   `provisional_reasons_for(faithfulness: float | None) -> set[ProvisionalReason]`
   (`{LOW_FAITHFULNESS}` below threshold, else empty; `None` → empty, the documented
   verifier-off mode). Migrate callers rather than keeping a bool wrapper.
2. `types/nodes.py::Proposition`: `provisional: bool | None` →
   `provisional_reasons: list[str]` (default `[]`; list for stable serialization,
   set semantics — dedupe on write).
3. `core/proposition.py`: persist `provisional_reasons` (AGE is schemaless — no
   migration) **and keep writing the legacy boolean** (`true` iff non-empty) for one
   transition release with a removal TODO; include reasons in extract `Action`
   outputs.
4. `grep -rn "provisional" src/ tests/` and migrate every reader.

Accept: low-faithfulness proposition persists `["low_faithfulness"]` + legacy
`true`; high-faithfulness persists `[]` + `false`; verifier-off persists `[]` +
`null`; no production read of the boolean except the legacy write. Tests:
`test_epistemic.py` (threshold edge), `test_proposition_layer.py` (persisted fields).

**Shipped** (commit `refactor(epistemic): R8 …`). One deviation worth recording: a
G1.14 **polarity-unstable twin** had no enumerated reason — it maps to
`LOW_FAITHFULNESS` (an instability on the same consistency/faithfulness axis, §3.1),
so no fourth member was invented. `_legacy_provisional(faithfulness, reasons)` keeps
the three-state boolean (`null` = unassessed). `reference.py` OR-folds
`UNRESOLVED_REFERENCE` via read-modify-write; `reuse.py` round-trips the JSON-string
list.

### R9 — quarantine gate function (pure) *(shipped)*

New module `src/iknos/core/quarantine.py`: `QuarantinedPropositionError`;
`Stakes(StrEnum)` `LOW`/`HIGH`; `assert_not_quarantined(proposition_reasons:
Collection[str], stakes: Stakes) -> None` — HIGH + non-empty reasons → raise
(message lists reasons); LOW always passes. Pure — no DB, no settings. Module
docstring states the call contract: every path that creates a `REFUTES`, or a
`SUPPORTS` that is the target's sole support, calls this with `Stakes.HIGH` before
writing. Tests (`test_quarantine.py`): the three-row truth table; importable
without `DATABASE_URL`.

**Shipped** (commit `feat(quarantine): R9 …`) exactly as specified — the error carries
the offending `reasons` for the caller's audit record.

### V7 — quarantine enforcement in the edge producer *(needs R8+R9) — shipped*

`core/edge_producer.py` is the live `SUPPORTS`/`REFUTES` creation site and never
consults provisional state. Read the module docstring + `plan_hypothesis` /
`build_evidence` / `produce` first; design intent is record-and-skip, never abort.

1. **Load reasons:** where the producer resolves each evidence node's `statement` +
   `effective_credibility`, also resolve its provisional reasons — a
   Fact/Conclusion inherits the union of `provisional_reasons` over the
   `Proposition`s it is `EVIDENCED_BY`. An evidence node with no proposition is
   treated as quarantined with reason `"missing_provenance"` + a warning log.
2. **Enforce at planning:** in `plan_hypothesis`, after the judge returns, derive
   stakes per would-be edge — `HIGH` for any `REFUTES` and for a `SUPPORTS` that
   would be the hypothesis's sole support in this plan; `LOW` otherwise — and call
   `assert_not_quarantined`. On raise: **drop the edge from the plan** (other
   hypotheses unaffected) and record it in the Action's `outputs.quarantined`:
   `{evidence_id, sign, reasons, stakes}` — a triage signal, not an error.
3. Pure helpers beside `edge_significance`/`build_evidence`; DB read joins the
   existing load. Extend the module-docstring invariants.

Accept: provisional-sourced REFUTES not persisted + recorded as quarantined; same
node may still drive a LOW-stakes SUPPORTS; sole-support SUPPORTS quarantined,
two-supporter case not; a quarantined edge never aborts the batch. Tests: unit
stakes table + plan-level drop; integration provisional→fact→produce→no REFUTES
edge + Action carries `quarantined`. Do not: filter at the candidate/judge stage
(the judge should still see the evidence — quarantine gates the *write*); touch
`qbaf_adapter.py` (V8).

**Shipped** (commit `feat(adjudication): V7 …`) as specified: `edge_stakes` +
`_load_provisional_reasons` + `QuarantineRecord`; `plan_hypothesis` drops and records;
`EdgeProductionResult.quarantined` surfaces the drops; result rows are built from the
*planned* edges so a dropped edge is never reported persisted; `qbaf_adapter.py`
untouched. The sole-support count is taken **before** any quarantine drop (so dropping
one provisional supporter does not retroactively promote another to "sole").

### V8 — `persist_verdicts` ensemble filter *(the G4.5 consumer-filter slice)*

G4.5 slice 1 shipped the gate's pure core (`core/ensemble_gate.py::authorise`,
unanimity-of-required + dissent veto, `DEFAULT_GATE` safe-by-default while the
symbolic channel ABSTAINs). `core/qbaf_adapter.py::persist_verdicts` still writes
whatever state it is given. Make the §7.2 invariant structural in the writer:

1. `persist_verdicts` gains `gate_decisions: Mapping[str, GateDecision]` (hypothesis
   id → slice-1 `authorise` result; default empty). For a verdict whose computed
   state is `refuted`: authorising decision → persist as today; otherwise → persist
   `acceptability` as computed, persist `state` as the hypothesis's **previous**
   state (read in the same query; none → `unsupported`), set
   `pending_refutation: true` on the vertex. Clear `pending_refutation` whenever a
   later verdict persists non-refuted or authorised-refuted.
2. Record the hold per the adapter's existing audit behavior (held-back ids +
   `reason: "ensemble_gate_pending"`); reuse the gate's `is_finding` notion — a
   withheld flip is a §13 finding, surfaced not smoothed.
3. Docstrings: "`refuted` is unreachable through this writer without an authorising
   `GateDecision`; `ensemble_gate.authorise` is the only intended producer." Plus
   the one-line §7.2 backport to `architecture.md` (see that file's §7.2).

Accept: no decision → acceptability persisted, state held, `pending_refutation`
set; authorising decision → today's behavior; later non-refuted verdict clears the
flag; no other code path writes `Hypothesis.state` (grep + assert in PR body).
Tests: unit hold/authorise/clear table (build `GateDecision`s through the real
`authorise` — don't mock the gate); integration evaluate→persist with and without
authorisation. Do not: modify `ensemble_gate.py`; build the symbolic/temporal
producers (later G4.5 slices); change `classify_state`.

### R4 — HNSW indexes on both pgvector tables + distance-operator standardization

No ANN index exists on `document_embeddings`/`proposition_embeddings`. New
migration (next free revision): `CREATE INDEX ... USING hnsw (embedding
vector_cosine_ops) WITH (m = 16, ef_construction = 64)` on both; downgrade drops
both; `op.execute` (no native alembic hnsw). Standardize on **cosine** (`<=>`)
— vectors are L2-normalized so cosine ≡ inner product; cosine chosen for
robustness if normalization drifts. Comment both ORM columns: "k-NN must use `<=>`
to hit the index." Accept: upgrade on fresh + populated DB; `EXPLAIN ... ORDER BY
embedding <=> $1 LIMIT 10` shows the hnsw index (`SET enable_seqscan = off` on
tiny tables); downgrade clean. Test: integration, mirroring the migration-test
style. Do not: change the 1024 dimension; add IVFFlat.

### V9 — pgvector k-NN push-down + recall-vs-exact measurement *(needs R4)*

`core/candidates.py::embedding_knn_candidates` is exact in-memory cosine — the
documented recall ceiling and the seam for the `<=>` push-down. Build the other
side of the seam:

1. DB-backed alternative in the adapter (the SQL lives with the other DB reads):
   per hypothesis `SELECT proposition_id FROM proposition_embeddings WHERE model =
   :model ORDER BY embedding <=> :vec LIMIT :k`, then map proposition → reasoning
   node via the same `EVIDENCED_BY` read. Same contract, same
   `CandidateSource.EMBEDDING_KNN`, no similarity floor, same deterministic
   tie-break; the `WHERE model =` clause is the G1.16 vector-space guard — never
   drop it.
2. Setting `CANDIDATES_KNN_PUSHDOWN: bool = False` (`config.py` + `.env.example`).
   **Default stays in-memory exact** — flipping it is a G4.6 decision.
3. The measurement: integration test with ≥200 synthetic normalized vectors —
   push-down ⊆ exact ranking at equal k, and EXPLAIN contains `hnsw`. The recall@k
   number on the real gate corpus is a one-line addition to the G4.6 run.
4. Comment on the query: "`<=>` must match the R4 opclass (`vector_cosine_ops`) or
   the index is unused."

Do not: flip the default; remove the in-memory path (it is the oracle); touch
`funnel` or the structural stage.
