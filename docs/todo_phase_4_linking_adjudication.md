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
(unanimity-of-required + universal dissent veto, `DEFAULT_GATE` decided by a fixture); the
gate's **consumer-filter landed (V8, `persist_verdicts`)** — a structural `refuted` is held
at its prior state + `pending_refutation` unless an authorising `GateDecision` is supplied,
so `refuted` is unreachable through the writer without the gate;
`corroborate`/`find-contradiction` operators + the gate's **temporal** channel producer
(rest of G4.5; the **symbolic** producer landed in W3, `core/symbolic_gate.py`) and the
validation gate (G4.6) open.**
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
R8 → R9 → V7 (quarantine enforcement in the edge producer) → V8 (the
`persist_verdicts` ensemble filter, i.e. the "consumer-filter" slice of G4.5,
wired to the slice-1 `authorise` and holding un-authorised flips as a
`pending_refutation` finding) — because the REFUTES creation site exists with
§3.1 quarantine unenforced, and `persist_verdicts` still writes whatever state
it is given. **Status: the safety lockdown is complete — R8, R9, V7 and V8 all
shipped; the edge producer now enforces §3.1 quarantine at the write.** And before G4.6 can run at all: the **gate assets** V1 (planted
corpus), V2 (gold labels — longest lead, start the annotator recruitment now),
V3 (metrics harness), plus the E1 baselines V4–V6 — specs in `todo_trials.md`.
The lockdown specs are in *Open task specs* below.

**Composed-loop spine (2026-06-11 architecture assessment, W1/W2/W3).** The
per-layer cores are verified but **the system has never run as a system**: nothing
calls the G3.9 `stabilize` driver, so the `REFUTES → retract → Layer A → Layer B →
QBAF → gate` feedback loop has no executable path and no test — and with the
symbolic channel ABSTAINing, `DEFAULT_GATE` withholds every automated `refuted`
flip, so the differentiator capability is currently *correct-but-non-functional*.
After the lockdown: **W1 (the composed-loop orchestrator) shipped** —
`core/revision_loop.py` drives `stabilize` over the retracted-node set
(load-once / pure-loop / persist-once), so an *authorised* refutation now retracts
and re-adjudicates to a fixpoint, and a non-converged region is surfaced as a §13
finding. **W2 (the synthetic §8 end-to-end fixture) shipped** —
`tests/integration/test_revision_loop_e2e.py` runs the §8 experiment on real AGE with
**zero LLM calls** (gate decisions injected through the real `authorise`): an overturning
fact retracts a supporting fact, the unfounded cycle drops while the grounded one stays
byte-stable, `hb` flips **only** through the gate (held → `pending_refutation`; authorised
→ refuted), and a crafted mutual-`REFUTES` region is surfaced as a §13 finding that
commits nothing — a permanent regression suite. **W3 (the interim refutation-gate decision)
shipped** — decided eyes-open as **option (a)**: `core/symbolic_gate.py` ships the minimal
clingo/ASP consistency producer for the gate's required SYMBOLIC channel, so `DEFAULT_GATE`
is now functional (an automated `refuted` flip is reachable on real LLM + symbolic agreement;
a symbolic dissent vetoes), no longer correct-but-dead. Specs in *Open task specs* below; findings
record in `archive/review_2026-06-11_planned_architecture_assessment.md`.

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
      *write*. *(Shipped, `#77`: `plan_hypothesis` derives `edge_stakes` per would-be
      edge and calls `assert_not_quarantined`; `_load_provisional_reasons` inherits the
      union of the evidence node's `EVIDENCED_BY` `Proposition` reasons (a node with no
      Proposition → `missing_provenance`); a quarantined edge is dropped from the plan
      and surfaced as a `QuarantineRecord` on the result + `Action.outputs.quarantined`,
      never aborting the batch. `qbaf_adapter.py` untouched.)*
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

## Open task specs *(merged from `archive/gap_review_2026-06.md` R4/R8/R9, `archive/gap_review_2026-06-11.md` V7/V8/V9, and `archive/review_2026-06-11_planned_architecture_assessment.md` W1/W2/W3 — execute as written; one task per PR, branch `fix/<id>-<slug>`)*

Work-stream order: **R8 → R9 → V7 → V8** (the safety lockdown — before the remaining
G4.5 slices), then **W1 → W2** (the composed-loop spine — before Phase 5 and before
the G4.6 run), with **W3** decided alongside the G4.5 channel-producer work, and
**R4 → V9** (gate ANN infrastructure — with the gate trials).
Migrations: set `down_revision` to the actual head (`alembic heads`) — numbering in
older specs is stale. **Safety lockdown complete: R8/R9/V7/V8 all shipped. W1
shipped; the composed-loop spine W1/W2/W3 all shipped.**

### R8 — `provisional` boolean → `provisional_reasons` set — ✅ **shipped**

*Shipped as `fix/r8-provisional-reasons`. One change vs. this spec: a fourth reason
`polarity_unstable` was added beyond the three below — the G1.14 polarity-twin
quarantine is a pre-existing extract-time cause (set independently of faithfulness so it
survives verifier-off mode, per `test_verify_all_failure_preserves_twin_provisional`) that
the spec's "known reasons" list omitted. `provisional_reasons_for(faithfulness)` still owns
only the `LOW_FAITHFULNESS` leg; the propositionizer contributes `POLARITY_UNSTABLE` and the
reference binder `UNRESOLVED_REFERENCE`, OR-folded via `merge_provisional_reasons`. The
legacy boolean is derived by `legacy_provisional(faithfulness, reasons)` (reproduces the
exact None/False/True tri-state); `reuse._reasons_from_props` reconstructs reasons for pre-R8
nodes so a replay never silently clears a quarantine.*

One flag currently carries several meanings; triage (§11.1) needs the reason, the
quarantine gate (R9) needs non-emptiness. Known reasons now: `low_faithfulness`
(Phase 1), `unassessed_faithfulness` (Phase 1 degraded mode — §3.1 D2, amended
2026-06-11), `unresolved_reference` (Phase 2), `uninferred_budget` (Phase 5).

1. `types/epistemic.py`: add `ProvisionalReason(StrEnum)` with those four values;
   replace `is_provisional(...)` with
   `provisional_reasons_for(faithfulness: float | None) -> set[ProvisionalReason]`
   (`{LOW_FAITHFULNESS}` below threshold; **`None` → `{UNASSESSED_FAITHFULNESS}`** —
   §3.1's decided rule: unassessed grounding is provisional, never coerced toward
   trusted; this changes the previously-documented verifier-off behavior, see G1.21
   in `todo_phase_1_ingest.md`; else empty). Migrate callers rather than keeping a
   bool wrapper. **Merge-order note (2026-06-11):** PR #67 implements R8 with the
   original three members and `None → set()` — written before this amendment
   landed. That is fine: merge #67 as-is; **G1.21 then delivers the fourth member
   and the `None → {UNASSESSED_FAITHFULNESS}` mapping** as the follow-up. Until
   G1.21 lands, the quarantine gate does *not* hold back unverified (verifier-off)
   propositions — the D2 rule in §3.1 is spec-ahead-of-code there.
2. `types/nodes.py::Proposition`: `provisional: bool | None` →
   `provisional_reasons: list[str]` (default `[]`; list for stable serialization,
   set semantics — dedupe on write).
3. `core/proposition.py`: persist `provisional_reasons` (AGE is schemaless — no
   migration) **and keep writing the legacy boolean** (`true` iff non-empty) for one
   transition release with a removal TODO; include reasons in extract `Action`
   outputs.
4. `grep -rn "provisional" src/ tests/` and migrate every reader.

Accept: low-faithfulness proposition persists `["low_faithfulness"]` + legacy
`true`; high-faithfulness persists `[]` + `false`; verifier-off persists
`["unassessed_faithfulness"]` + legacy `true` (the G1.21 behavior change — update
the pinned degraded-mode tests deliberately); no production read of the boolean
except the legacy write. Tests:
`test_epistemic.py` (threshold edge), `test_proposition_layer.py` (persisted fields).

**Acceptance amendment (2026-06-11 architecture assessment, P4).** The in-flight
R8 implementation (`fix/r8-provisional-reasons`) also carries
**`POLARITY_UNSTABLE`** (the G1.14 polarity-twin reason — add it to the member
list above; a twin is provisional *independent of faithfulness*), but ships
**no fixtures for the invariants the set carries** — the highest-risk gap the
assessment found, since this is the quarantine gate's data model. R8 is *not
complete* until these tests exist: a polarity twin seeds `POLARITY_UNSTABLE` and
**stays provisional even when verified faithful** (a passing verify must not
clear the OR-fold); a twin that is also low-faithfulness carries **both** reasons
(OR-fold union, never overwrite); reasons survive an AGE persist → read
round-trip; and the legacy boolean mirrors non-emptiness in each case.

### R9 — quarantine gate function (pure) — ✅ **shipped**

*Shipped as `fix/r9-quarantine-gate`, to spec. `assert_not_quarantined` normalises
`proposition_reasons` through `merge_provisional_reasons` (one source of truth for the
reason-list shape) and gates via a `_GATES_ON_PROVISIONAL` dict keyed on every `Stakes`
member, so a future calibrated middle band is a fail-loud KeyError, not a silent pass. The
raised `QuarantinedPropositionError` carries the normalised `reasons` tuple + `stakes` so the
V7 caller records the `{evidence_id, sign, reasons, stakes}` triage signal straight off the
exception. `test_quarantine.py`: the three-row truth table + the move-not-atom asymmetry,
importable without `DATABASE_URL`.*

New module `src/iknos/core/quarantine.py`: `QuarantinedPropositionError`;
`Stakes(StrEnum)` `LOW`/`HIGH`; `assert_not_quarantined(proposition_reasons:
Collection[str], stakes: Stakes) -> None` — HIGH + non-empty reasons → raise
(message lists reasons); LOW always passes. Pure — no DB, no settings. Module
docstring states the call contract: every path that creates a `REFUTES`, or a
`SUPPORTS` that is the target's sole support, calls this with `Stakes.HIGH` before
writing. Tests (`test_quarantine.py`): the three-row truth table; importable
without `DATABASE_URL`.

### V7 — quarantine enforcement in the edge producer *(needs R8+R9)* — ✅ **shipped**

*Shipped as `fix/v7-quarantine-edge-producer` (`#77`), to spec. Extracted as the
V7-only slice onto current main — R8 (`#72`)/R9 (`#73`) having merged separately, the
combined `#67` was unmergeable (its own R8/R9 commits conflicted with the merged ones)
and was closed/superseded. `_load_provisional_reasons` resolves each evidence node's
reasons as the **union** over its `EVIDENCED_BY` `Proposition`s (a node with none →
`MISSING_PROVENANCE` + a warning); pure `edge_stakes` marks any `REFUTES` and a
sole-support `SUPPORTS` `HIGH` (count taken **before** drops, so dropping one supporter
can't promote another to "sole"); `plan_hypothesis` calls `assert_not_quarantined` per
would-be edge and on raise drops it, recording a `QuarantineRecord` on the result +
`Action.outputs.quarantined`. Result rows are built from the **planned** edges so a
dropped edge is never reported persisted. `qbaf_adapter.py` untouched (V8). Unit stakes
table + plan-level drop + end-to-end produce; integration provisional→fact→produce→no
`REFUTES` edge + Action carries `quarantined`.*

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

### V8 — `persist_verdicts` ensemble filter *(the G4.5 consumer-filter slice)* — ✅ **shipped**

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

**Shipped** (commit `feat(adjudication): V8 …`) as specified. `persist_verdicts` gains
`gate_decisions` and returns a `PersistResult` (`written` + `held`, `is_finding`) instead
of a bare count — the held refutations are the surfaced §13 finding. Pure
`refutation_held(state, decision)` is the decision seam (unit-tested through the real
`authorise`, gate not mocked). Two deviations worth recording: (a) the previous state is read
with a **separate current-row query** (`_load_state`), not a `coalesce`-in-`SET` — AGE's
`SET`-expression support is uncertain and a read-then-write in the caller's transaction is
unambiguously correct (no other writer touches `Hypothesis.state`, verified by grep); (b) the
clear-on-non-hold is done by **always writing `pending_refutation`** (`true` on a hold, `false`
otherwise) so any non-refuted/authorised verdict lifts a prior hold. `ensemble_gate.py` and
`classify_state` untouched; the §7.2 one-liner is backported to `architecture.md`.

### R4 — HNSW indexes on both pgvector tables + distance-operator standardization — **shipped**

No ANN index exists on `document_embeddings`/`proposition_embeddings`. New
migration (next free revision): `CREATE INDEX ... USING hnsw (embedding
vector_cosine_ops) WITH (m = 16, ef_construction = 64)` on both; downgrade drops
both; `op.execute` (no native alembic hnsw). Standardize on **cosine** (`<=>`)
— vectors are L2-normalized so cosine ≡ inner product; cosine chosen for
robustness if normalization drifts. Comment both ORM columns: "k-NN must use `<=>`
to hit the index." Accept: upgrade on fresh + populated DB; `EXPLAIN ... ORDER BY
embedding <=> $1 LIMIT 10` shows the hnsw index (`SET enable_seqscan = off` on
tiny tables); downgrade clean. Test: integration, mirroring the migration-test
style. Do not: change the 1024 dimension; add IVFFlat. **Shipped** as specified
(migration `0013_embedding_hnsw_indexes`): HNSW `vector_cosine_ops` (`m=16,
ef_construction=64`) on both tables via `op.execute`, **mirrored in `iknos.db.orm`
`__table_args__`** (pgvector-sqlalchemy `postgresql_using='hnsw'`) so `alembic check`
is drift-clean; both `embedding` columns carry the `<=>` note; `tests/integration/
test_embedding_hnsw_indexes.py` asserts the index exists + the planner uses it for a
`<=>` k-NN. Verified end-to-end on live pgvector 0.8.2 (upgrade → EXPLAIN Index Scan →
downgrade clean). **V9** (the push-down query + recall-vs-exact measurement) is the
consumer that builds on it.

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

### W1 — composed-loop orchestrator (the missing spine) *(needs V7+V8; P1) — shipped*

The pure cores are individually verified, but nothing owns the cross-layer control
flow: `core/composed_loop.py::stabilize` (G3.9) is implemented, tested, and never
called, so retraction never triggers re-adjudication — changes are only picked up
on the next independent read. Phase 5 belief revision and the V8 consumer-filter
need the same sequencing; without one owner there will be divergent ad-hoc wirings.

1. New module `src/iknos/core/revision_loop.py` (adapter layer — may import DB +
   pure cores): the step body `retract → Layer A (derivation_adapter) → Layer B →
   QBAF (qbaf_adapter.evaluate) → ensemble gate (authorise) → persist_verdicts
   (the V8 filter)`, driven by `composed_loop.stabilize` with its iteration bound
   and oscillation surfacing. An authorised `refuted` flip that retracts a fact
   feeding the refuter re-enters the body — the §12
   composition-with-retraction-feedback this driver exists for.
2. Non-convergence is a **finding** (§13): surface the unstable sub-region with
   its subgraph; never silently re-iterate. Every iteration appends an `Action`
   (§10.1).
3. Keep it thin: no VoI, no re-inference budget (Phase 5/6 layers); single working
   box; invoked explicitly, no daemon.

Accept: a fixture where an authorised `REFUTES` retracts a supporting fact and
the loop re-runs A → B → QBAF to a fixpoint within the bound; an oscillating
fixture surfaces `is_finding` with the unstable region; `stabilize` is the only
loop driver (grep: no ad-hoc retry loops around `qbaf_adapter`). Do not: build
the symbolic/temporal channel producers here (W3 / later G4.5); add
incrementality beyond the existing delta loads.

**Shipped** (commit `feat(reasoning): W1 …`). Design recorded so it is not
re-litigated: because `stabilize` is **pure/synchronous** but the graph work is
async, the loop is **load-once → run the pure step in memory over the
retracted-node set → persist-once at the fixpoint**, so `stabilize` stays the sole
driver and the iteration touches no DB. The step uses the pure cores
(`support_and_confidence` for A/B, `assemble_baf`/`adjudicate` for the QBAF — *not*
the async `qbaf_adapter.evaluate`, which reads graph confidence; the loop feeds the
recomputed Layer B confidence as the QBAF base in-memory and writes it back on
convergence). Two injectable seams (the user-confirmed fork): `decide` (gate
decisions — the LLM/symbolic/temporal channels live outside; W2 injects decisions
through the real `authorise`) and `revise` (the §12 retraction policy; **default**:
retract each authorised-refuted hypothesis). Persistence runs **only on
CONVERGED** — `persist_verdicts` (V8 filter) + retractions (`valid_to`) + the Layer
B confidence write-back (a node that lost grounding is written **0**, not left
stale); a non-converged loop commits nothing and is returned as the finding.
Shared graph loaders were extracted to module functions (`load_evidential_edges`,
`load_base_fact_ids`, `load_derived_rows`) so the loop re-assembles the *same*
active subgraph the adapters do. Tests: unit (pure step — A/B-propagation,
gate-gated retraction, convergence, oscillation, divergence) + integration
(authorised refutation retracts the supporting fact → c dropped → fixpoint; the
unauthorised case held with `pending_refutation`).

### W2 — synthetic end-to-end fixture: the §8 experiment in test form *(needs W1; 2026-06-11 assessment, P1)* — ✅ **shipped**

*Shipped as `feat/w2-revision-loop-e2e`, to spec. Two tests in
`tests/integration/test_revision_loop_e2e.py`, green on real AGE with **zero LLM calls**
(gate decisions injected through the real `authorise`, never mocked). The fixture is two
**structurally disjoint** regions in one box: Region A (a grounded cycle `p↔q` grounded by
a never-retracted fact `gf` + a hypothesis `ha`) and Region B (an unfounded-after-retraction
cycle `x↔y` grounded only by `bf` + a hypothesis `hb` weakly supported by `x` and strongly
refuted by an overturning fact `r`). One test runs two passes on the same seed — **pass 1
no decision** (the structural refutation is held: `pending_refutation`, nothing retracted —
and it settles Region A's annotations as the byte-stability baseline), **pass 2 authorised**
(the §12 policy retracts `bf`): asserts (a) Region A is **byte-for-byte unchanged** (full
property maps equal — retraction stayed local), (b) the unfounded cycle `x,y` drops to
confidence 0 while the grounded cycle survives, (c) `hb` flips to `refuted` **only** through
the authorised gate, (e) the returned `Action` ids reconstruct the retracted-set trajectory
(§10.2). A second test (d) crafts a mutual-`REFUTES` region under an oscillating revision
policy and asserts the loop **surfaces** the §13 finding (`unstable_region`) and commits
**nothing** (`persisted is None`, the graph byte-stable), the terminal `Action` carrying the
unstable region. Kept as a permanent regression suite — red if any A→B→QBAF→gate→persist
seam is rewired. (Disjoint-region byte-stability is the locality oracle; `mypy` is scoped to
`src/iknos` so the test's untyped agtype helpers are consistent with the W1 integration test.)*

The architecture's own must-pass (§8 *Proposed small-scale experiment*) exists in
no form — every correctness guarantee currently rests on per-layer unit tests.
W2 is the code-level precursor to the V1 gate corpus: V1 is real documents + gold
labels measuring *accuracy*; W2 is a hand-built graph fixture proving *mechanics*
— cheap, deterministic, zero LLM calls (judgments injected as pre-built
opinions/`GateDecision`s through the real `authorise`).

1. Integration test (`tests/integration/test_revision_loop_e2e.py`): seed base
   facts → derive conclusions (incl. one grounded cycle and one
   unfounded-after-retraction cycle) → 2–3 hypotheses with `SUPPORTS`/`REFUTES`
   → inject the overturning fact.
2. Assert: retraction propagates and **stays local** (an untouched region's
   annotations are byte-stable); the unfounded cycle drops while the grounded one
   survives; hypothesis state flips **only** through the gate (no decision →
   `pending_refutation`; authorised → flip); a crafted mutual-`REFUTES` region
   hits the iteration bound and is surfaced, not smoothed; every change is
   walkable through `Action`s (§10.2).
3. Keep it as a permanent regression suite (the gate section already promises
   this for the planted corpus; W2 is its mechanical half).

Accept: green on the ephemeral AGE DB with zero LLM calls; red if any layer seam
(A → B → QBAF → gate → persist) is rewired without it noticing. Do not:
substitute it for V1–V3.

### W3 — interim refutation-gate decision (clingo producer vs explicit LLM-only) *(2026-06-11 assessment, P3)* — ✅ **shipped (option a)**

*Decided eyes-open (confirmed with the user): **option (a) — ship the minimal clingo
symbolic-channel producer**, shipped as `feat/w3-symbolic-gate-clingo`. `core/symbolic_gate.py`
is the pure clingo/ASP consistency engine over a typed `SymbolicQuery` (the analogue of the pure
`qbaf.py`/`subjective_logic.py` cores before their AGE adapters): `check_consistency` runs the
"is the hypothesis ∧ the refuter logically inconsistent under the box's rules?" test — **UNSAT →
CONTRADICTORY → AFFIRM**, related-but-**SAT → CONSISTENT → DISSENT** (the veto guard the channel
exists for), **unrelated / already-inconsistent → ABSTAIN** (honest insufficiency, never a
disguised dissent). The encoding closes **transitively** through the sub-region's rules, which is
why clingo earns its place over a two-atom set test. `symbolic_channel_for(query)` produces the
`ChannelSignal` a consumer passes straight into `authorise_from_panel(..., symbolic=…)`, so
`DEFAULT_GATE`'s required SYMBOLIC channel is now wired and an automated `refuted` flip is reachable
on genuine LLM + symbolic agreement. The atom **identity** (which propositions are "the same claim",
so opposite polarity is a `P`/`¬P` twin) is the embedding twin-cluster the perception layer already
computes (`core/consistency.py`, G1.14); the DB adapter that builds a `SymbolicQuery` from the
active sub-region is the documented consuming seam (the `find-contradiction` operator). `clingo>=5.8`
added to `pyproject.toml`/`uv.lock`. Tests (`tests/unit/test_symbolic_gate.py`, clingo not mocked):
the verdict table incl. the transitive case + the attribution guards, and — the W3 requirement —
**which gate is in force**: with the real symbolic channel wired, `DEFAULT_GATE` now AUTHORISES on
LLM+symbolic AFFIRM, a symbolic DISSENT vetoes, a symbolic ABSTAIN still withholds, and the
no-symbolic-signal default stays safe-by-default. `ensemble_gate.symbolic_channel()` remains that
ABSTAIN seam (its docstring now points to the real producer). **Note:** option (b)'s
`LLM_ONLY_GATE` is retained at the seam; the gate choice is a value, so a deployment can still opt
into it explicitly.*

With `SYMBOLIC` required and its producer unwired (ABSTAIN), `DEFAULT_GATE`
withholds **every** automated `refuted` flip — safe-by-default per principle 6,
but it leaves the differentiator capability silently non-functional: it looks
implemented and does nothing. Decide eyes-open, record the outcome in the status
block and the `todo.md` deferred-triggers table, one of:

- **(a) Ship the minimal symbolic producer:** a clingo consistency check over the
  affected sub-region (the G4.5 channel-producer slice pulled forward), unblocking
  `DEFAULT_GATE` as designed; or
- **(b) Adopt `LLM_ONLY_GATE` explicitly** for the gate-trial window: logged
  rationale, a revisit trigger ("the symbolic producer lands, or G4.6 measures the
  single-channel false-refutation rate"), and the gate choice stamped on every
  gate `Action` so post-hoc audit can distinguish the regimes.

Either way, a unit test pins the *chosen* default's behavior — today no test
asserts which gate is in force. Do not: leave the choice implicit in
`DEFAULT_GATE`'s definition.
