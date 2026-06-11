# Gap Plan — Phase 4 (Evidence Linking & Adjudication)

**Why this file exists.** `todo_phase_4_linking_adjudication.md` is the requirement list
(referencing `architecture.md` by §); this file is the **build plan** — the increment
breakdown (G4.x), the design decisions taken, and the sequencing — mirroring
`gap_phase_3_reasoning_core.md`. `architecture.md` (§5 the edge model, §5.1 candidate
generation, §7.2 the hypothesis state machine + ensemble gate, §8 the belief-revision design
space + edge-judgment disciplines, §11.2 the verdict bands) remains the source of truth.

**Depends on:** Phase 2 (nodes, `SUPPORTS`/`REFUTES` edge targets) and **Phase 3 Layer B**
(the `[0,1]` confidence that is the QBAF's intrinsic/base score, §12). Built as a thin slice
alongside Phase 3 — the *pure* adjudication core (G4.1) depends on nothing but the abstract
BAF; everything that touches real data or an LLM depends on the candidate/judgment/persistence
increments below.

## Build order (decision → pure engine → candidates → judgment → persistence → gate)

The same `todo.md` discipline as Phase 3: a **decision made with a fixture before the engine**
(here the gradual semantics, as the Layer B semiring was in G3.5), then the pure engine generic
over it, then the data- and LLM-bound layers that feed it, closing with the **validation gate**
that must pass before anything is hardened (§8 experiment, principle: "do not harden any layer
until the gate passes").

| ID | Increment | Depends on | State |
|----|-----------|------------|-------|
| **G4.1** | **QBAF gradual-semantics adjudication core** — the semantics decision (DF-QuAD vs Quadratic Energy) + the pure `solve` engine (bounded fixpoint, non-convergence surfaced) + verdict banding / computed hypothesis state | Phase 3 Layer B (contract only) | **shipped (this increment)** |
| G4.2 | **Candidate generation** (§5.1) — the cheap→expensive funnel: structural priors (shared `INVOLVES`, co-occurrence), embedding k-NN over pgvector, coarse-to-fine over §2 levels; tuned for recall early | Phase 1 embeddings, Phase 2 graph | planned |
| G4.3 | **Edge-judgment pipeline** (§8) — sign-before-magnitude, blind + randomized, multi-sample consistency, per-model recalibration, subjective-logic opinion + source discounting, cumulative/averaging fusion → calibrated `SUPPORTS`/`REFUTES` `strength` (never the raw LLM number) | G4.2, an LLM seam | planned |
| **G4.4** | **QBAF persistence adapter** — load the active `SUPPORTS`/`REFUTES` subgraph + hypothesis base scores (Layer B) from AGE → `BAF`; write the computed `acceptability` / `state` back to the `Hypothesis` node. The Phase-4 analogue of G3.4 | G4.1, Phase 2/3 adapters | **shipped (this increment)** |
| G4.5 | **`corroborate` / `find-contradiction` operators + ensemble gate** (§7.2) — gather supporting/refuting evidence; `find-contradiction` as a first-class refuter generator; the **ensemble gate** (multi-sample LLM + symbolic + temporal agreement) that authorises a persisted `refuted` flip; wires the `REFUTES→retract→A→B→QBAF` body into the G3.9 `stabilize` driver | G4.3, G4.4, G3.9 | planned |
| G4.6 | **Validation gate** (§8 experiment) — planted-contradiction corpus (regression suite); measure retraction propagation, hypothesis-state flip, consistency-vs-verbalized confidence, ensemble-vs-single, candidate/refuter recall, level-attachment accuracy; bias-controlled scoring, not LLM-as-judge | G4.2–G4.5 | planned |

Cross-cutting: the stored edge `strength` is **never** the raw LLM number (§8, §10) — it is the
fused/recalibrated/expert-correctable value the QBAF consumes; and the hypothesis `state` /
`acceptability` are **computed, never hand-set** (§10). G4.1 fixes the consuming end of that
contract (the engine + the read-off); G4.3/G4.4 fix the producing end.

## G4.1 — QBAF gradual-semantics adjudication core (this increment)

**What shipped.** `core/qbaf.py` — the pure, in-memory adjudication core (no DB, no AGE, no
LLM, no migration), the Phase-4 analogue of Layer B's `core/confidence.py`. Three parts, in the
Phase-3 order:

**1. The semantics decision (G4.1's G3.5-style fixture).** §8 names two gradual semantics and
they are not interchangeable; the choice is **epistemic**, so it is made with a numeric fixture
*before* the engine is trusted:

- **`GradualSemantics`** is the algebra-as-a-value (mirroring `Semiring`): an `aggregate`
  (fold a bag of per-edge contributions into one quantity) and a `combine`
  `(base, aggregate_support, aggregate_attack) → strength`. The two operations are a matched
  pair stored as plain functions, so `solve` is written **once, generic over the value**, and
  the default is swapped at the seam — not branched on.
- **`DF_QUAD`** — probabilistic-sum aggregation (`a ⊕ b = a + b − a·b`), discontinuity-free
  combination; **saturates**. **`QUADRATIC_ENERGY`** (Potyka) — plain-sum *energy*
  `E = Σsupport − Σattack` squashed by `φ(x)=x²/(1+x²)`; **accrues**.
- **Decision, recorded eyes-open: `DEFAULT_SEMANTICS = DF_QUAD`.** The fixture
  (`test_qbaf_semantics.py::test_decision_fixture_…`) shows the two **rank the same two
  hypotheses oppositely**: one *strong* supporter (contribution 0.9) vs three *weak* ones
  (0.4 each) → DF-QuAD ranks the strong one higher (saturation), Quadratic Energy ranks the
  weak trio higher (accrual). DF-QuAD is the **conservative** default under the standing §13
  risk that **correlated LLM error is not removed by the edge-judgment disciplines**: it will
  not let several correlated weak "supports" manufacture a high acceptability. It is also
  bounded + discontinuity-free *by construction* and ordering-preserving (matching §8 and the
  ordinal Gödel Layer B feeding it). This parallels the Layer B choice (Gödel over Viterbi):
  default to the algebra that **cannot inflate**; **retain** the other at the seam for a
  decorrelated sub-domain. The choice stays reversible.

**2. The engine.** `solve(baf, *, base, semantics, max_iterations, tolerance) -> QbafResult`
computes acceptability as a **tolerance-bounded fixpoint** by synchronous (Jacobi) sweeps. One
edge contributes `edge.strength · σ(src)` — the §7.1 edge weight modulating the source's
*current* strength — so a weak edge or weak source lends little. Design decisions taken up
front:

- **The base score is the Layer B confidence (§12 seam), supplied as a side map** (like
  `valuate`'s `base_confidence`), never stored on the `BAF` — the structure stays independent
  of the scoring, and the read-and-evaluate adapter (G4.4) fills it. A node absent from the map
  defaults to `0.0` (no intrinsic support until evidenced); seeding `solve` at the base scores
  makes a node with no edges an immediate fixpoint (**stability**).
- **Non-convergence is a finding, not a hang (§13).** Acyclic frameworks converge to the exact
  fixpoint; cyclic ones (mutual `SUPPORTS`/`REFUTES`) have **no general guarantee**, so the loop
  is bounded and, on hitting the bound, returns `converged=False` with the still-moving
  arguments in `QbafResult.unstable` — the unresolved region the caller surfaces (`is_finding`),
  **never silently re-iterated or smoothed into a verdict**. This is the *inner-numeric*
  analogue of the *outer* composed-loop driver `core/composed_loop.py::stabilize` (G3.9): there
  discrete states + exact recurrence; here continuous strengths + a tolerance. Base/edge scores
  outside `[0,1]` raise (a cheap boundary check, since the combination math assumes the unit
  interval).

**3. The read-off (§7.2, §10, §11.2).** `VerdictBands.band` maps acceptability into the §11.2
graded verdict (`true / plausible / implausible / false`); `classify_state` computes the
hypothesis `state` (`supported` clears the bar; below it, `refuted` if net attack dominates,
else `unsupported`) — **computed, never hand-set**. The bands are **data** (a swappable value),
defaulting to placeholder cut-points that are **calibration targets for the validation gate**
(G4.6), so calibration re-points them without touching the engine.

**Tests** (`tests/unit/`, DB-free; 34 new, 497 unit total). `test_qbaf_semantics.py` — the
decision fixture (the opposite-ranking headline + the saturation-vs-accrual contrast) and the
gradual-argumentation **properties both semantics satisfy**: stability, balance (equal
support/attack ⇒ base), neutrality (zero edge/source ⇒ no-op), monotonicity (support raises,
attack lowers), boundedness on `[0,1]`, anonymity (edge-order independence), dangling-edge
tolerance. `test_qbaf_adjudication.py` — the engine (base-only one-sweep; acyclic chain to a
hand-computed fixpoint; cyclic mutual support converging to a bounded fixpoint with no
inflation; **non-convergence surfaced** under a tight bound, and the same framework converging
under a generous one; determinism; bad-bound / out-of-range rejection); the **Layer B seam**
(acceptability computed from evidence, moving away from the base, not the raw base); and the
read-off (verdict banding at the cut-points; supported/refuted/unsupported; a custom support
bar). ruff + `ruff format` clean; mypy(`src/iknos`) clean (only the pre-existing
`resolve.py:159` remains, not ours).

**Deferred (documented seams, not regressions):**

- **Candidate generation (G4.2), the edge-judgment pipeline (G4.3), and the AGE persistence
  adapter (G4.4)** — this increment is the pure engine + decision; it takes a `BAF` and a base
  map and returns acceptability/state. Producing calibrated edges from LLM judgments and loading
  the active subgraph from AGE are the data-/LLM-bound increments that feed it.
- **The ensemble gate (§7.2)** — `classify_state` computes the *structural* `refuted` the QBAF
  implies, but §7.2 mandates a flip *to* `refuted` be authorised by the ensemble gate
  (multi-sample LLM + symbolic + temporal agreement). That gate is G4.5; the engine's finding is
  the input to it, not a licence to persist a flip.
- **The composed-loop body** — wiring `REFUTES→retract→A→B→QBAF` into `stabilize` (G3.9) needs
  `find-contradiction` + retraction feedback (G4.5). G4.1 supplies the QBAF step that body will
  call each pass.
- **Incremental QBAF update** — §13 flags this as an apparent open research gap (no published
  algorithm for incrementally updating final strengths under graph change). Incrementality
  stops at Layer A's delta; the affected QBAF sub-region is recomputed in full (acceptable at
  investigation scale, §13). `solve` is that full recompute.

## G4.4 — QBAF persistence adapter (this increment)

**What shipped.** `core/qbaf_adapter.py` — the boundary that reads the persisted AGE graph into
the pure G4.1 engine and writes the verdict back, the Phase-4 analogue of G3.4's
`derivation_adapter`. Same pure/DB split (pure assembly + evaluation, DB only in the `async`
methods, lazy `iknos.db.age` import).

**Opened by reconciling a G4.1 duplication (recorded so it is not repeated).** G4.1 had
re-declared `HypothesisState` and a `Verdict` banding policy (same 0.75/0.5/0.25 cut-points)
inside `core/qbaf.py`, duplicating `types/intentional.py`'s `HypothesisState` + `AcceptabilityBand`
+ `band` — the single source of truth that module's docstring says the QBAF should *consume*.
The dedup commit makes `qbaf.py` import that vocabulary; `classify_state`'s support bar is now the
§11.2 `plausible` boundary via `band()` (no second policy), and `aggregate_evidence` was extracted
so `classify_state` can be fed the real per-node support/attack. Banding tests stay in
`test_intentional.py`, not duplicated.

**Design decisions taken up front:**

- **The two inputs are the §12 seam.** `base = the node's Layer B `confidence`` (the QBAF
  intrinsic score), `SUPPORTS`/`REFUTES` edges carry the §7.1 `strength`. Kept as separate maps,
  never merged. The reused `load_reasoning_nodes` supplies the same node confidence Layer B
  produced, so the seam is literally the same number.
- **Edge direction is the schema's (§5, §10): Fact/Conclusion (evidence) → Hypothesis.** So the
  edge `source` lends strength and `target` receives it (`Edge.src`/`.dst`); the **sign** routes
  to the support vs attack collection (§8 "sign before magnitude" — categorical, modelled
  structurally). The sign is taken from *which relationship type matched* (one query per type, as
  AGE matches a single label per pattern), the canonical source of direction.
- **Active-subgraph selection, dead-endpoint drop.** Bitemporally-current (`valid_to IS NULL`),
  active-box nodes/edges only; an evidential edge with an inactive endpoint is **dropped** — a
  retracted/deprecated-box supporter lends nothing. This is the *opposite* polarity to the
  derivation adapter, which keeps an inactive antecedent in a conjunctive body so the rule gets
  *harder*; QBAF support is additive, so a vanished supporter must contribute nothing. Recorded
  because the asymmetry is easy to get wrong.
- **Write-back is a partial `SET`, and the band is not stored.** `persist_verdicts` writes
  `h.acceptability` + `h.state` with a targeted `SET h.acceptability=…, h.state=…`, **not**
  `merge_vertex`'s full `SET n = {…}` (which would clobber the node's bitemporal/confidence
  fields — the integration test asserts `confidence` survives the write). The presentation
  `band` is **not** persisted (§11.2 / `intentional.py`: computed from the strength at render
  time, never a stored substitute for the real value).
- **De-dup of the shared reads.** `load_active_box_ids` / `load_reasoning_nodes` were extracted as
  module functions in `derivation_adapter` and are reused here, so the "active box" definition and
  the node-confidence read cannot diverge between the propagation and adjudication loads.

**Tests.** `tests/unit/test_qbaf_adapter.py` (DB-free): assembly (arguments + base map, sign
routing, active-box gating, dead/dangling-edge drop, determinism) and adjudication (supported /
refuted / unsupported, acceptability over all args, hypothesis-outside-subgraph skip).
`tests/integration/test_qbaf_adapter.py` (real AGE): `evaluate` computes the verdict and a
deprecated-box + a retracted supporter are both correctly excluded (don't inflate it);
`persist_verdicts` writes `acceptability`/`state` back **without clobbering** `confidence`; and a
**retraction** of the sole supporter lowers acceptability back to the base. ruff + `ruff format` +
mypy(`src/iknos`) clean (only the pre-existing `resolve.py` error remains); 538 unit tests pass.

**Deferred (documented seams, not regressions):**

- **The edge-judgment pipeline (G4.3)** that *produces* calibrated `SUPPORTS`/`REFUTES` edges — this
  adapter *consumes* them; the contract is exercised here with hand-built fixtures (as G3.4 defined
  the `DERIVED_FROM` contract before G3.8 wrote it).
- **The ensemble gate (§7.2)** — `persist_verdicts` writes every verdict it is given; gating a flip
  *to* `refuted` on ensemble agreement is the caller's filter (G4.5), kept out of the dumb writer.
- **Incremental / `SAME_AS`-canonicalized loads** — full current-state read over raw nodes (§13).

## Phase risks / decisions (carried from §8, §13)

- **Cyclic structure is surfaced, not forced to converge** (principle 8, §13). The QBAF gradual
  semantics has no general convergence guarantee on cyclic argument graphs — so the requirement
  is bound + detect + surface, *not* guarantee a fixpoint. G4.1 discharges this for the
  inner-numeric loop; the outer composed loop is G3.9 + G4.5.
- **LLM→QBAF weight mapping is unstandardized** (§8, §13) — turning a calibrated LLM judgment
  into a base score and an attack/support edge has no reference recipe; designed/validated in
  G4.3 against the planted corpus (G4.6). G4.1 fixes only the *consumption* of those numbers.
- **Correlated LLM error is not removed by the disciplines** (§13) — the DF-QuAD default is the
  conservative hedge against it at the aggregation layer; the disciplines (multi-sample, varied
  judges, flagging suspiciously uniform strengths) are G4.3.
- **Sign before magnitude** (§8) — direction is modelled *structurally* (which edge collection),
  separate from magnitude, so a wrong sign is categorical (catastrophic, guarded first) while a
  noisy magnitude is absorbed by the gradual semantics.
