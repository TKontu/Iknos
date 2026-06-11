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
complete across both cheap stages (G4.2 slice 1 structural + slice 2 embedding k-NN);
edge-judgment scoring core + blind LLM judge landed (G4.3 slice 1 subjective-logic algebra +
slice 2 blind/randomized judge); AGE edge producer / operators / gate open.**
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
the DB-free LLM layer between the funnel and the read-off; the AGE producer that writes the
surviving edges is the next slice. **G4.2 slice 1**
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
seams. The rest of the edge-judgment pipeline (§8, G4.3 — per-model recalibration and the AGE
producer that persists the judged `SUPPORTS`/`REFUTES` edges), the `corroborate` /
`find-contradiction` operators + ensemble gate (§7.2, G4.5), and the validation gate (§8, G4.6)
are open. See `gap_phase_4_linking_adjudication.md` for the build plan.

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
- [ ] Write `SUPPORTS`/`REFUTES` edges carrying `sign`, fused/recalibrated `strength`,
      and `significance` (from the node/tier). Stored `strength` is **never** the raw
      LLM number (§10).
- [ ] `corroborate` operator: hypothesis → gather supporting/refuting evidence.
- [ ] `find-contradiction` operator + **ensemble gate** (multi-sample LLM + symbolic +
      temporal agreement) required before any `REFUTES` (§7.2).

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
      `SET`; band derived-not-stored). **Open:** the flip *to* `refuted` requires the ensemble
      gate (§7.2, G4.5) — `persist_verdicts` writes what it's given, the caller filters.)*
- [x] Bound iteration + detect oscillation on cyclic argument graphs; surface
      unresolved regions rather than forcing convergence (principle 8, §13). *(G4.1 — `solve`
      bounds the fixpoint iteration and, on hitting the bound, returns `converged=False` with
      the still-moving arguments in `QbafResult.unstable` (`is_finding`) — surfaced, never
      smoothed into a verdict. Period-true oscillation over *discrete* loop states is the outer
      `core/composed_loop.py::stabilize` driver, G3.9.)*

## Validation gate (§8 experiment) — run before hardening anything

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
