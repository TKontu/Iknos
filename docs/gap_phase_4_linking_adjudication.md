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
| G4.2 | **Candidate generation** (§5.1) — the cheap→expensive funnel: structural priors (shared `INVOLVES`, co-occurrence), embedding k-NN over pgvector, coarse-to-fine over §2 levels; tuned for recall early | Phase 1 embeddings, Phase 2 graph | **slice 1 shipped** (the recall-first funnel core + the structural-entity prior, `core/candidates.py`); embedding k-NN + coarse-to-fine + keyword co-occurrence planned |
| G4.3 | **Edge-judgment pipeline** (§8) — sign-before-magnitude, blind + randomized, multi-sample consistency, per-model recalibration, subjective-logic opinion + source discounting, cumulative/averaging fusion → calibrated `SUPPORTS`/`REFUTES` `strength` (never the raw LLM number) | G4.2, an LLM seam | **slice 1 shipped** (the subjective-logic confidence-scoring core, `core/subjective_logic.py`); LLM judge + recalibration + AGE producer planned |
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

## G4.2 — Candidate generation, slice 1: the recall-first funnel + the structural-entity prior

**What shipped.** `core/candidates.py` — the cheap→expensive **blocking funnel** (§5.1) that
decides *which `(evidence → hypothesis)` pairs to assess*, so the expensive §8 LLM judgment (G4.3)
runs only on survivors instead of all-pairs `O(n²)`. This slice lands the **funnel core** + the
**first cheap generator (stage 1, the structural-entity prior)**; the other cheap stages union in
behind the same contract (seams below). Same pure/DB split as `core/qbaf_adapter.py` (pure value
types + funnel/stage logic, DB only in the adapter's `async` methods, lazy `iknos.db.age` import).

**1. The funnel-combination decision (G4.2's G4.1/G4.3-style fixture).** §5.1 mandates *recall
early, precision late* — a missed candidate is a silent false negative (an edge never considered),
a spurious one is just cheaply rejected at adjudication. The choice of how to **combine** the cheap
generators is therefore made with a fixture *before* the funnel is trusted:

- **`FunnelStrategy`** is the combine-operator-as-a-value (mirroring `GradualSemantics` / `Fusion`):
  `UNION` (a pair *any* generator proposes survives, sources merged) vs `INTERSECT` (only pairs
  *every* generator proposed). `funnel` is written **once, generic over it**; the default is swapped
  at the seam, not branched on.
- **Decision, recorded eyes-open: `DEFAULT_STRATEGY = UNION`.** The fixture
  (`test_candidates.py::test_decision_fixture_…`) is the §5.1 **dissimilar-refuter problem**: a
  refuting fact can be semantically *dissimilar* to the hypothesis it attacks, so embedding-NN
  under-generates refutation candidates and biases the system toward finding support and missing
  contradiction. The structural prior catches that refuter (it shares the hypothesis's entity);
  **`INTERSECT` would drop it** (the embedding stage never proposed it), re-introducing exactly that
  bias. So `UNION` is the conservative, recall-first default — *default to the operator that cannot
  lose a true candidate; retain the other at the seam* (`INTERSECT`, a precision pre-filter for a
  recall-saturated sub-domain), exactly parallel to the Layer-B (Gödel), QBAF (DF-QuAD) and fusion
  (averaging) choices. Reversible — a value, not a branch.

**2. The stage + the funnel.** `structural_entity_candidates` (the **near-free** stage 1, §5.1):
two reasoning nodes sharing an `Actor`/`Object` via `INVOLVES` (role-agnostic co-occurrence) are a
candidate, directed **evidence → hypothesis** (the schema direction a `SUPPORTS`/`REFUTES` edge
would run, §5/§10). A `Candidate` is **unscored** (recall-first: no ranking, that is the §8 LLM
stage's job) and carries provenance — which `CandidateSource`s proposed it + the `shared_entities`
rationale the later *relative* judgment can rank within. `funnel(*generators, strategy=…)` merges
all generators by `(evidence, hypothesis)` pair (unioning sources/entities), deterministically
sorted (stable trace, §10).

**3. The boundary.** `CandidateGenerationAdapter.generate` reads the active subgraph and runs the
funnel — reusing the shared `load_active_box_ids` / `load_reasoning_nodes` / `load_hypothesis_ids`
reads (the last **extracted** from `qbaf_adapter` this increment so the "current Hypothesis"
definition is single-sourced across adjudication and candidate generation, mirroring the existing
active-box / node-confidence de-dup). It partitions the active reasoning nodes into hypotheses (the
targets) vs evidence (Fact/Conclusion), scopes the `INVOLVES` rows to that active universe (§9
box-scoping carries through), and folds stage 1 through `funnel`.

**Tests** (`tests/unit/test_candidates.py` DB-free, 12 new; `tests/integration/test_candidates.py`
real AGE, 2 new). Unit: the decision fixture (union keeps the dissimilar refuter / intersect drops
it), the structural prior (shared-entity pairing, no-shared→none, multi-entity→one candidate,
evidence→hypothesis direction only, inactive-node ignore) and the funnel (source/entity merge,
determinism, empty). Integration: `generate` proposes the entity-sharing pairs (the dissimilar
refuter among them — the §5.1 recall guarantee), excludes the unrelated fact, and excludes a
deprecated-box supporter (active-box scope). ruff + `ruff format` + mypy(`src/iknos`) clean (only
the pre-existing `resolve.py` error remains); 607 unit tests pass.

**Deferred (documented seams, not regressions) — the rest of G4.2:**

- **Embedding nearest-neighbour (stage 2, the workhorse)** — each node's pgvector k-NN as
  relatedness candidates; sublinear, reusing the dense index. Deferred because it needs the
  cross-store read (the vectors live in relational `document_embeddings` / `proposition_embeddings`,
  the nodes in AGE) + the span/proposition → reasoning-node tracing. Unions in as an
  `EMBEDDING_KNN` `CandidateSource` without a contract change.
- **Coarse-to-fine (stage 3)** — reuse the §2 multi-level chunk hierarchy as a pruning tree (match
  coarse, descend within survivors). Needs the `partOf` level derivation (§14).
- **Sparse/keyword co-occurrence** — the other half of stage 1 (the `PropositionLexicalIndex`);
  a further `STRUCTURAL_KEYWORD` source that unions at the same seam.
- **`find-contradiction` as a first-class refuter *generator*** — this slice is the structural
  *half* of the dissimilar-refuter mitigation (pull by constituent entity); the dedicated
  contradiction-search operator is G4.5.
- **Conclusion-as-target** — slice 1 generates evidence → `Hypothesis`; pairing evidence against a
  `Conclusion` target (also valid per §5) is an additive extension of the partition.

## G4.3 — Edge-judgment pipeline, slice 1: the subjective-logic confidence-scoring core

**What shipped.** `core/subjective_logic.py` — the pure, in-memory algebra behind **steps 3–4**
of the §8 confidence pipeline (encode each judgment as a subjective-logic opinion with
source-reliability discounting; fuse with cumulative/averaging, never raw Dempster's rule). The
Phase-4 analogue of G4.1 (pure engine) and `core/confidence.py` (Layer B math): no DB, no AGE,
no LLM, no migration — a value algebra unit-testable with hand-built opinions, the in-house
re-implementation of the subjective-logic operators (QBAF-Py / Uncertainpy / Jøsang's library
are **reference only**, §8 Tooling). Three parts, in the Phase-3/G4.1 order:

**1. The fusion decision (G4.3's G3.5/G4.1-style fixture).** §8 names *two* fusion operators
(cumulative *or* averaging) and they are not interchangeable, so the choice is made with a
numeric fixture *before* the pipeline is trusted:

- **`Fusion`** is the operator-as-a-value (mirroring `GradualSemantics`): a `name` + a binary
  `fuse_pair`, so `fuse` is written **once, generic over the operator**, and the default is
  swapped at the seam — not branched on. The two instances are `CUMULATIVE` (aleatory; assumes
  **independent** sources; *accrues* certainty) and `AVERAGING` (epistemic; assumes possibly
  **dependent** sources; **idempotent** — does not accrue).
- **Decision, recorded eyes-open: `DEFAULT_FUSION = AVERAGING`.** The fixture
  (`test_subjective_logic.py::test_decision_fixture_…`) shows three *correlated copies* of one
  weak judgment fuse **back to that one judgment** under averaging (no manufactured certainty)
  but **collapse the uncertainty and climb the belief** under cumulative (false confidence). The
  standing §13 risk is that **correlated LLM error is not removed by the disciplines** — blind,
  randomized, multi-sample judgments from one model are *not* independent — so averaging is the
  **conservative** default: it cannot inflate certainty from correlated judges. This parallels
  the Layer B (Gödel over Viterbi) and QBAF (DF-QuAD over Quadratic Energy) choices — *default
  to the operator that cannot inflate; retain the other at the seam* (`CUMULATIVE`, for a
  genuinely decorrelated varied-model sub-domain). Reversible — a value, not a branch.

**2. The operators.** The binomial `Opinion` `(belief, disbelief, uncertainty, base_rate)`
(validated frozen value; `belief+disbelief+uncertainty == 1`, all on `[0, 1]`), with:
`projected_probability = belief + base_rate·uncertainty` (the read-off — **this is the
calibrated edge `strength`** the QBAF consumes); `opinion_from_evidence(positive, negative)` —
the **multi-sample-consistency → opinion** map (Beta/binomial, non-informative prior weight `W`:
agreement raises belief, more samples shrink uncertainty — consistency *is* certainty, §3.1 at
the edge layer); `discount(opinion, reliability)` — SL **trust discounting** (the §8 ↔ §9.1
seam: each opinion is discounted toward uncertainty by its source's `effective_credibility`
before fusion); and `cumulative_fuse` / `averaging_fuse` / `fuse`.

- **The vacuous/neutral asymmetry (recorded so it is not got wrong).** The vacuous opinion is
  the **neutral element of cumulative** fusion but **not of averaging** — averaging weights each
  opinion by its uncertainty mass, so an abstaining (or fully source-discounted) judge *dilutes
  toward uncertainty* rather than being silently dropped. The more-conservative behavior, and
  part of why averaging is the default. Both-dogmatic (`uncertainty == 0`) inputs share a
  documented equal-weight-average limit (one helper, so the two operators cannot diverge on it).
- **Bounds are enforced, not clamped.** Out-of-range masses/counts/reliability *raise* (the
  `epistemic.combine_faithfulness` / `credibility` convention), and fusion requires a shared
  base rate (fusing opinions about *different* propositions is a caller bug, surfaced).

**3. The read-off (the QBAF seam).** `projected_probability` of the fused, discounted opinion is
the calibrated `strength` ∈ [0, 1] that replaces the raw LLM confidence (§8, §10). `core/qbaf.py`
already names this upstream ("subjective-logic fusion has already decorrelated the evidence");
this slice supplies it. **Sign** stays structural and categorical — the `SUPPORTS` vs `REFUTES`
edge type (§10), decided first and separately (§8 "sign before magnitude") — so this slice scores
*magnitude* for a sign already fixed.

**Tests** (`tests/unit/test_subjective_logic.py`, DB-free; 29 new, 595 unit total). The decision
fixture (averaging idempotent vs cumulative accrues on correlated evidence; the independent-
supporters flip side); opinion validity + projection; the consistency→opinion map (agreement→
belief, more samples→less uncertainty, zero observations→vacuous, bad inputs reject); discounting
(full=identity, zero=vacuous, partial, out-of-range rejects); and the fusion properties the
pipeline relies on (cumulative neutrality of vacuous, averaging dilution, commutativity,
averaging idempotency, fused-opinion validity, single=identity, empty/base-rate-mismatch reject).
ruff + `ruff format` + mypy(`src/iknos`) clean (only the pre-existing `resolve.py` error remains).

**Deferred (documented seams, not regressions) — the rest of G4.3:**

- **The LLM judge** (next slice) — **sign-before-magnitude** (classify supports/refutes/
  irrelevant first and separately; estimate magnitude only for non-irrelevant edges), **relative
  not absolute** (elicit by ranking competing evidence on the same hypothesis), **blind +
  randomized** (judge blind to the current hypothesis state — sycophancy guard; randomize
  evidence order across samples — position-bias guard). That prompted elicitation produces the
  per-sample counts `opinion_from_evidence` consumes; this slice is the scoring algebra that
  consumes them (the pure/LLM split, exactly as G4.1(pure)/G4.4(AGE)).
- **Per-model recalibration (step 2)** — a *fitted* per-model consistency→correctness curve with
  no data yet; like the `combine_faithfulness` calibration seam and the G4.1 verdict bands, it
  swaps in at `opinion_from_evidence` (scaling the evidence) or post-projection without a contract
  change. Identity until G4.6 fits it against the planted corpus.
- **The AGE producer** (next slice) — writing the `SUPPORTS`/`REFUTES` edge carrying the fused
  `strength` + `significance` (from the node/tier, §9) and an `Action` (raw judgment + sampling +
  calibration, §10.1); the data-bound increment that consumes this read-off, the Phase-4 analogue
  of how `derivation_adapter` (G3.4) consumes `core/confidence.py`.

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
