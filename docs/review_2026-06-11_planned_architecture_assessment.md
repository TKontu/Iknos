# Architecture Assessment — Plan vs. Implementation (2026-06-11)

Independent review of the planned architecture (`docs/architecture.md`) and the shipped
implementation (`src/iknos`, Phases 0–4 core). Assesses scope, effectiveness, viability,
and structure; identifies issues that will punish later; recommends the significant
changes needed for the system to work properly and perform well.

Relationship to prior reviews: `docs/archive/review_2026-06-11_post_phase4_review.md`
and `docs/archive/gap_review_2026-06-11.md` already raised the validation-gate gap (F1)
and the unenforced safety gates (F2), tracked as V1–V9 / R8–R11. This review confirms
those, does not re-litigate them, and adds findings they did not cover. Findings new in
this review are marked **[NEW]**.

---

## 1. Verdict summary

**The planned architecture is unusually rigorous and internally coherent — the primary
risk is not design error but design weight.** The plan resolves its hard problems
correctly (two-layer propagation forced by algebra, gradual QBAF adjudication,
defeasible identity, three separated confidence types, fail-loud caching). The shipped
code is faithful to it: the pure cores (Layer A DRed, Layer B Gödel semiring, QBAF
DF-QuAD, subjective logic, ensemble gate) are implemented and unit-tested to high
fidelity.

The three findings that matter most:

1. **The system has never run as a system.** Every layer is individually verified;
   the composed loop that defines the product — REFUTES → retract → Layer A → Layer B →
   QBAF → gate — has no orchestrator and no end-to-end test. `stabilize()` exists and is
   never called. **[NEW]**
2. **The code is far ahead of the evidence.** Phases 0–4 core are shipped; zero
   validation-gate assets exist (no corpus, no gold labels, no harness, no baselines).
   The architecture itself names "building the full system before the baseline check"
   as the expensive failure mode (§13) — and that is the current trajectory.
   (Already tracked as F1/V1–V6; restated here because it dominates everything else.)
3. **Refutation — the differentiator — is currently non-functional end to end.**
   The ensemble gate's symbolic channel ABSTAINs (no clingo producer), so the default
   gate withholds every automated `refuted` flip; and the quarantine + gate
   consumer-filters at the live REFUTES creation site are unenforced (F2/V7/V8). The
   system can currently find support but cannot complete a refutation. **[partly NEW]**

None of these require redesign. They require a deliberate stop on new feature slices
and a pivot to integration + validation work that is already (mostly) on the books.

---

## 2. Scope

**Right-sized target, honestly bounded.** The stated scale — tens of dense case
documents per investigation against an amortized static reference corpus (§6.1) — is
the correct ambition level, and the plan repeatedly derives engineering decisions from
it (in-memory network analysis, full-recompute QBAF, exact in-memory k-NN). The scale
assumptions are *stated*, which makes them checkable; §13 explicitly flags what reopens
if the scale assumption breaks.

**Scope creep risk lives in the long tail of subsystems.** The plan now spans:
parse front-end, windowed embedding, multi-level segmentation, propositionizing with
five epistemic fields, faithfulness verification, mention binding, entity resolution,
candidate generation, edge adjudication, two-layer propagation, QBAF, ensemble gating,
bitemporality, boxes/tiers, domain packs, governance (sensitivity lattice + conditional
credibility + adversarial-source defenses), VoI triage, Task framing, leads/inquiries,
part-whole hierarchy with meronymy typing, and the mixed-level frontier. Each is
individually justified; collectively they are several person-years. The plan's own
defense — the E1 go/no-go baseline check plus ablation ("a fraction of the system may
capture most of the value", §13) — is the right control, **but it only works if it runs
early, and it has not run.** Until E1 exists, scope is unbounded in practice regardless
of what the document says.

**Two items are research, not engineering, and are correctly labeled as such:** the
significance-weighted mixed-level frontier and the fact→referent level operator (§14).
They are appropriately deferred behind evaluation gates and nothing shipped depends on
them. Keep them out of the critical path.

**One scope omission [NEW]:** the plan has no explicit *de-scoping ladder* — a stated
minimal configuration that still constitutes a sellable/usable system if the gate
results are mixed (e.g., "extraction + faithfulness + provenance + hybrid retrieval,
without QBAF/Layer A-B" as a fallback product). The ablation (E2) implies one but the
plan never names it. Writing it down now would make a partial gate failure survivable
rather than existential.

---

## 3. Effectiveness — will it do what it claims?

**The mechanisms are correct where they have been tested, which is per-layer only.**

What is verified (unit/fixture level, with file references):

- Layer A well-founded support incl. grounded-vs-ungrounded cycle fixtures and
  incremental DRed (`src/iknos/core/truth_maintenance.py:162–404`;
  `tests/unit/test_truth_maintenance.py:118–333`).
- Layer B Gödel `max-min` fixpoint gated on Layer-A-certified membership
  (`src/iknos/core/confidence.py:124–200`) — matches the §12 decision exactly.
- QBAF DF-QuAD with convergence/oscillation surfacing (`src/iknos/core/qbaf.py:191–330`).
- Sign-before-magnitude, blind, randomized, multi-sample edge judging
  (`src/iknos/core/edge_judge.py`), with averaging fusion as the
  correlation-conservative default (`src/iknos/core/subjective_logic.py:305`).
- The perception layer: macro-windowed embedding with the
  furthest-from-edge pooling rule (`src/iknos/core/embeddings.py:35–306`), structured
  epistemic fields end-to-end, the decided `verify × calibrate(agreement)` combiner with
  identity reductions (`src/iknos/types/epistemic.py:221–257`), three-state null
  faithfulness, and verification keyed as its own cached stage (`src/iknos/core/cache.py:51–98`).

What is *not* verified, and gates the effectiveness claim:

- **The composed loop (§12 termination discipline) is unwired.** `composed_loop.py`
  implements fixpoint/oscillation/divergence detection (lines 95–165) and nothing in
  `src/` calls it. There is no orchestrator implementing the
  retract → A → B → QBAF → gate step body, so retraction does not trigger
  re-adjudication; changes are only picked up on the next independent read. The
  architecture's headline non-monotonic claim ("a newly extracted fact can overturn an
  earlier conclusion") has no executable path and no test. **[NEW]**
- **The planted-corpus experiment (§8) — the architecture's own must-pass — does not
  exist** even in synthetic fixture form: no test seeds facts → conclusions →
  hypotheses, injects an overturning fact, and asserts the cascade. The cycle-safety
  guarantees rest entirely on per-layer unit tests.
- **Refuter recall (the dissimilar-refuter problem, §5.1/§13) is unmeasured.** The
  structural candidate stage that is supposed to mitigate it is implemented
  (`src/iknos/core/candidates.py:212–260`) and union-merged recall-first — good — but
  the plan itself says refuter recall "must be measured, not assumed," and it has not been.
- **Differentiator axes vs. baselines (E1) untested** — the go/no-go has no rig (V4–V6).

**Conclusion on effectiveness:** the system is *plausibly* effective and nothing found
contradicts the design's reasoning — but every claim that distinguishes it from plain
RAG (refutation, retraction, calibrated confidence) is currently either unwired,
withheld by an abstaining gate, or unmeasured. Effectiveness is an open empirical
question by the plan's own standard, and the plan's instrument for answering it (the
gate) is the least-built part of the project.

---

## 4. Viability

**Storage engine: viable with caveats, and the plan already knows the caveats.** The
single-engine choice (Postgres + AGE + pgvector) is sound for the target scale, and the
GIN-on-properties indexing decision was made empirically against what the planner
actually emits (migration `0007`, verified by `tests/integration/test_age_label_indexes.py`)
— a model practice. Residual viability risks, in order:

- **AGE under property density is still unbenchmarked.** §13 calls this "a real
  viability risk that 'engine: chosen' should not paper over"; Trial C3 has not run.
  Every vertex now carries 15–20 properties in one `agtype` column. The GIN index backs
  id/box lookups, but compound filters (`box AND sensitivity_level`) and *edge-property*
  filters (`SAME_AS {state: 'confirmed'}`) have no index path and will sequential-scan.
  Phase 3+ belief revision and Phase 5 re-scoring will hit exactly those shapes. **[partly NEW]**
- **Dual-write atomicity is assumed, not enforced [NEW].** Graph writes go through raw
  Cypher (`execute_driver_sql`) and relational writes through the ORM on the same
  session — one transaction in the happy path, but there is no rollback discipline at
  call sites. A failed Fact write after a persisted Action leaves the audit log
  pointing at an artifact that does not exist — corrupting the very auditability the
  architecture makes a first-class constraint (principle 9).
- **In-process, in-memory compute model has fired its trigger.** Entity resolution and
  candidate generation load whole working sets into Python
  (`src/iknos/core/resolve.py:409–456`, `candidates.py:278–352`); embeddings run
  in-process (torch in the worker); exact cosine k-NN, no ANN index. All acceptable at
  MVP scale and all *recorded* as deferred — but R4/V9 and R10/R11 are marked **FIRED**
  in the todo deferral table and remain unstarted. The first real multi-document corpus
  ingest (V1, the gate corpus!) is the trigger, so the gate work itself will collide
  with this debt.
- **Operational viability is the thinnest area.** Auth/authz, backup/restore,
  observability, and the API contract are acknowledged as Phase 6–7 entry documents
  and none are written. Fine for now; becomes the bottleneck the moment a second user
  or a real case appears.

**Cost viability is designed, mostly built, and credible:** content-addressed extraction
caching with full pipeline identity in the key, verification as a separate cached stage,
fail-loud `StaleExtractionError` — shipped as specified. The VoI-gated re-inference
budget is Phase 6 and absent, which is fine while ingest volumes are tiny.

---

## 5. Structure

**The module structure is one of the strongest aspects of the implementation.**

- **Pure-core / adapter split, consistently applied.** `truth_maintenance`, `confidence`,
  `qbaf`, `subjective_logic`, `ensemble_gate`, `composed_loop`, `consistency` have zero
  DB/LLM imports; `derivation_adapter`, `qbaf_adapter`, `edge_producer` own the
  boundary. No circular imports found. This is what makes the per-layer test fidelity
  possible, and it should be defended.
- **Schema truth is genuinely centralized** in `src/iknos/types/` (epistemic,
  temporal, governance, intentional vocabularies; thresholds defined once). ~95% of the
  §10 contract exists in code. AGE DDL is manual-by-design in migrations.
- **The seams the plan demands are real seams in code:** Layer A → Layer B → QBAF
  hand-off is implemented exactly as the §12 contract describes; the
  faithfulness/credibility/strength separation is reflected in module boundaries.

Structural weaknesses, in order of future pain:

- **The missing orchestrator is a structural hole, not just a missing feature [NEW].**
  Adapters exist per layer but nothing owns the cross-layer control flow. Today each
  consumer wires `support_and_confidence()` → `qbaf_adapter` ad hoc; when the composed
  loop, the ensemble gate consumer-filter, and belief revision (Phase 5) all need the
  same sequencing, the absence of a single `investigation`/`revision` orchestration
  module will produce three divergent wirings. This module should exist *before*
  Phase 5, and the F2 enforcement work (V7/V8) is the natural occasion to create it —
  the gate consumer-filter is precisely an orchestration concern.
- **Cypher query construction is safe-in-practice, not safe-by-construction [NEW].**
  `cypher_map()` and the dollar-quote machinery in `db/age.py` are sound, but ~140 call
  sites interpolate labels/edge types/UUIDs/timestamps into f-strings outside that
  helper (e.g. `resolve.py:423`, `qbaf_adapter.py:273`, `component_aggregate.py:255–257`,
  `reference.py:654`). All current values come from enums/UUIDs/`isoformat()` so there
  is no live injection, but the pattern offers no defense in depth and one future call
  site with a user-influenced value breaks it silently. A small query-builder layer
  (typed label/edge enums + value escaping, single chokepoint) would convert a
  convention into a guarantee.
- **Manual serde pairs are replicated drift points [NEW].** `Sensitivity.flatten()/
  from_props()`, `SourceInterest`, box serde, `same_as_to_props()` — each hand-written
  pair is a place where the write format and read format can diverge without any test
  failing. Round-trip property-based tests per serde pair are cheap insurance.
- **Schema changes fan out 4–6 places** (types model → prompt → output schema →
  props serializer → reader → tests). Tolerable now; worth a checklist in CONTRIBUTING
  rather than machinery.

---

## 6. Issues that will punish later (ranked)

Severity ranks integration debt above code defects, because the defects found are
small and local while the integration gaps are load-bearing.

| # | Issue | Where | Why it punishes |
|---|-------|-------|----------------|
| P1 | **Composed loop unwired; no end-to-end retraction/refutation path or test** [NEW] | `composed_loop.py` (never called); no planted-corpus fixture | The product's defining behavior is unproven; Phase 5 (belief revision) would be built on a loop that has never executed. Integration bugs discovered then are rework of Phases 3–4. |
| P2 | **Validation gate has zero assets while code advances** (known: F1) | V1–V6 unstarted; V2 gold labels are the longest-lead item | Every further slice deepens sunk cost against an unverified go/no-go. The architecture names this exact failure mode. |
| P3 | **Refutation safety gates unenforced at live REFUTES site; symbolic channel ABSTAINs** (known: F2; channel detail NEW) | `edge_producer.py` (no provisional/faithfulness filter); `ensemble_gate.py:366` (SYMBOLIC abstains → DEFAULT_GATE withholds all flips) | Either unsafe (if LLM_ONLY_GATE is chosen casually) or non-functional (default withholds everything). Both quietly undermine the headline capability. |
| P4 | **R8 (`provisional_reasons`) landed with no tests** [NEW] | `epistemic.py:129–173`, OR-fold at `proposition.py:510–512`; zero fixtures for POLARITY_UNSTABLE, LOW_FAITHFULNESS, OR-fold, persistence round-trip | This is the quarantine gate's data model. An untested regression here ships confidently-wrong atoms — the precise failure §3.1 exists to prevent. Blocks R9/V7. |
| P5 | **Multi-sample agreement is trivially perfect at temperature 0** [NEW] | defaults `temperature: 0.0` in `proposition.py:55`, `extract.py:288`, `verify.py:105`; no validation when `n_samples > 1` | §3.1 says "the configuration must enforce this, not document it." N identical samples → agreement = 1.0 → inflated faithfulness, silently. One `__init__` guard fixes it. |
| P6 | **Extraction cache key uses context *text* without context span identity** [NEW] | `cache.py:54`, context assembly `proposition.py:330–337` | A re-segmentation that changes the K-span context window can serve stale extractions or thrash the cache unpredictably. Include context span ids in the key. |
| P7 | **Dual-write (graph + relational + Action log) has no rollback discipline** [NEW] | raw-Cypher + ORM on one session; no try/rollback patterns at operator call sites | Rare failure → audit log references missing artifacts → principle-9 auditability silently broken. Cheap to fix now, miserable to retrofit. |
| P8 | **Fired deferral triggers unstarted: ANN index, out-of-process embeddings, job queue** (known: R4/V9, R10/R11) | no pgvector HNSW/IVFFlat migration; torch in-process | The trigger is the gate corpus ingest itself — this debt lands exactly when the most important work (V1–V3) starts. |
| P9 | **AGE density benchmark (C3) never run; edge-property filters have no index path** [partly NEW] | migration 0007 covers vertex GIN + edge endpoints only | Phase 5 re-scoring queries will sequential-scan edge tables; if AGE fundamentally can't carry the density, finding out after Phase 5 means a storage migration under load. |
| P10 | **Cypher f-string footprint; manual serde pairs** [NEW] | ~140 interpolation sites; `governance.py:71–106` etc. | Not broken today; each is a single-future-mistake-away class of silent corruption or injection. |

Minor (noted, not ranked): embedding-model mismatch detected only at write time after
expensive work (`embeddings.py:23–32`); whitespace/zero-vector span skips not
per-reason audited (`ingest.py:100–116`); verifier-down degraded mode logged but not
surfaced to triage; hard-coded agreement threshold (`consistency.py`); `Mention` absent
from `types/nodes.py` while `reference.py` documents the binding cascade — the Phase 2
seam should be visible in the schema module, not only in prose.

---

## 7. Significant changes recommended

**No architectural redesign is needed.** The design decisions audited (two-layer
propagation, Gödel default, DF-QuAD, averaging fusion, multiplicative faithfulness
combiner, conservative entity-resolution defaults, GIN indexing) are each justified,
fixture-decided where the plan demanded it, and correctly implemented. The changes
below are about sequencing, integration, and hardening — not design.

1. **Stop feature slices; build the spine.** Implement the composed-loop orchestrator
   (the retract → Layer A → Layer B → QBAF → gate step body driving `stabilize()`,
   ~50–150 lines given the adapters exist) **and** the §8 planted-corpus synthetic
   fixture that exercises it: seed facts → conclusions → 2–3 hypotheses → inject the
   overturning fact → assert retraction cascade, hypothesis flip, gate behavior,
   oscillation surfacing. This single test converts the architecture's central claim
   from "designed" to "demonstrated" and is the prerequisite for Phase 5 regardless of
   gate outcomes.
2. **Close the refutation path deliberately.** Land R8 tests → R9 → V7 (quarantine
   enforcement in the edge producer) → V8 (ensemble-gate consumer-filter in
   `persist_verdicts`), and make an explicit, recorded decision on the interim gate:
   either ship the minimal clingo symbolic-consistency producer (unblocking
   DEFAULT_GATE) or consciously adopt LLM_ONLY_GATE with a logged rationale and a
   trigger to revisit. The current state — refutation silently withheld — is the worst
   of both: it looks implemented and does nothing.
3. **Fund the gate as the top project priority** (confirms prior reviews): start V2
   (gold labels — longest lead, human annotators) immediately and in parallel with
   V1/V3/V4. Treat P8 (job queue, out-of-process embeddings, ANN index) as part of the
   gate workstream since V1 ingest fires those triggers.
4. **Three small hardening fixes, each ≤ a day, each closing a silent-corruption
   class:** (a) enforce nonzero temperature when `n_samples > 1` (constructor
   validation); (b) add context span ids to the extraction cache key; (c) adopt a
   write-then-commit discipline (or transactional wrapper) covering graph write +
   relational write + Action append, with rollback tests.
5. **Convert Cypher safety from convention to chokepoint.** A thin query-builder over
   `db/age.py` (validated label/edge enums, mandatory value escaping) and a lint/grep CI
   rule against raw f-string Cypher outside it. Add round-trip property tests for every
   manual serde pair.
6. **Run C3 (AGE density benchmark) before Phase 5**, with the realistic property load
   and the edge-property query shapes belief revision will actually emit. The §13
   fallback (separate graph store) is vastly cheaper to take before bitemporal
   supersession logic is built on AGE than after.
7. **Write the de-scoping ladder into the plan [NEW]:** name the minimal viable
   configurations the E2 ablation will compare (e.g., perception layer + provenance +
   hybrid retrieval alone; + Layer A/B; + QBAF; + gate), so a mixed E1 result has a
   pre-agreed landing zone instead of triggering an unstructured rethink.

---

## 8. Bottom line

- **Scope:** ambitious but honestly bounded and stated; controlled only if the E1
  go/no-go actually runs early. Add a written de-scoping ladder.
- **Effectiveness:** mechanisms correct per-layer with strong fixtures; the
  system-level claim (non-monotonic refutation/retraction) is currently unwired,
  withheld, and unmeasured — open by the plan's own standard.
- **Viability:** sound at target scale; real risks are AGE-under-density (unbenchmarked),
  dual-write atomicity, and fired-but-unstarted infrastructure triggers that collide
  with the gate work.
- **Structure:** excellent pure-core/adapter discipline and centralized schema truth;
  needs the missing cross-layer orchestrator, a Cypher chokepoint, and serde round-trip
  guarantees.
- **Will it punish later?** Yes, in one specific way: every week of new feature slices
  before the composed-loop spine + gate assets exist increases the cost of whatever the
  gate reveals. The punishing issues are integration and validation debt, not design
  flaws — which is the best version of this finding, because it is fixable by
  sequencing, not rework.
