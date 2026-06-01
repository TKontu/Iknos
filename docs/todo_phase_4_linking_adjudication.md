# Phase 4 — Evidence Linking & Adjudication

**Goal:** connect evidence to hypotheses with well-judged edges and adjudicate
hypothesis state. Contains the hardest judgment (connection strength) and the most
bias-prone step, so it is heavily disciplined. Closes with the **validation gate**.

**Depends on:** Phase 2 (nodes), Phase 3 (Layer B confidence). Built in parallel with
Phase 3 as a thin slice.
**Architecture refs:** §5 (edge model), §5.1 (candidate generation), §8 (edge
disciplines, confidence pipeline, experiment), §7.2 (ensemble gate, hypothesis state),
§10 (`sign`/`strength`/`significance`).

## Candidate generation (§5.1) — which pairs to assess

- [ ] Funnel, cheap → expensive; **two stages separate from adjudication**.
- [ ] Structural priors: shared `Actor`/`Object` (`INVOLVES`), sparse/keyword
      co-occurrence; box/tier-scoped. Near-free, filters the bulk.
- [ ] Embedding **k-NN** over pgvector (approximate NN, sublinear) — the workhorse.
- [ ] **Coarse-to-fine** over the §2 abstraction levels: match coarse, descend to
      proposition pairs only within survivors.
- [ ] **Tune for recall early, precision late** — a missed candidate is a silent
      false negative.
- [ ] **Dissimilar-refuter handling:** hypotheses pull candidates by constituent
      entities + topic, not similarity alone; `find-contradiction` is a first-class
      generator, not a similarity by-product.

## Edge adjudication (§8 disciplines) — the bias-hardened judgment

- [ ] **Sign before magnitude:** classify direction (supports/refutes/irrelevant)
      first; estimate strength only for non-irrelevant edges.
- [ ] **Relative, not absolute:** elicit strength by ranking/pairwise comparison of
      competing evidence on the same hypothesis.
- [ ] **Blind + randomized:** judge blind to current hypothesis state (sycophancy
      guard); randomize evidence order across samples (position-bias guard).
- [ ] **Multi-sample consistency**, per-model recalibration, encode as subjective-logic
      opinion with source discounting, fuse with cumulative/averaging (not raw
      Dempster's rule).
- [ ] Write `SUPPORTS`/`REFUTES` edges carrying `sign`, fused/recalibrated `strength`,
      and `significance` (from the node/tier). Stored `strength` is **never** the raw
      LLM number (§10).
- [ ] `corroborate` operator: hypothesis → gather supporting/refuting evidence.
- [ ] `find-contradiction` operator + **ensemble gate** (multi-sample LLM + symbolic +
      temporal agreement) required before any `REFUTES` (§7.2).

## Adjudication (QBAF)

- [ ] Model supports/refutes as a **Quantitative Bipolar Argumentation Framework**;
      Layer B confidence is the base score.
- [ ] Gradual semantics (DF-QuAD or Quadratic Energy), in-house (QBAF-Py/Uncertainpy
      as reference only).
- [ ] **Hypothesis state machine:** compute supported/unsupported/refuted +
      `acceptability` from incoming evidence; state is computed, never hand-set (§10).
- [ ] Bound iteration + detect oscillation on cyclic argument graphs; surface
      unresolved regions rather than forcing convergence (principle 8, §13).

## Validation gate (§8 experiment) — run before hardening anything

- [ ] Build the planted-contradiction corpus (sources with conflicts + a later
      overturning fact); keep as a regression suite.
- [ ] Measure: retraction propagation (Phase 3); hypothesis-state flip on the
      overturning fact; consistency vs verbalized confidence; ensemble vs single-call
      contradiction; **candidate recall, especially refuter recall**.
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
