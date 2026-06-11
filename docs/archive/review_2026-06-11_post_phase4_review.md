# Architecture & Project Review — 2026-06-11 (post-Phase-4-core)

> **Status: findings record.** Third review pass. The two June 2026 reviews
> (`review_2026-06_architecture_and_plan.md`, `review_2026-06_architecture_plan.md`,
> remediation in `gap_review_2026-06.md`) found the foundational defects; this review
> verifies what actually got fixed, audits the code shipped since (Phases 2–4:
> G2.1–G2.8, G3.1–G3.9, G4.1–G4.3), and identifies the problems that are live *now*.
> Method: full read of `architecture.md` and `todo.md`; three parallel audit passes
> over (a) `src/iknos` incl. all Phase 2–4 modules, (b) all phase/gap/trial docs
> reconciled against git history, (c) tests, migrations, CI, and infra; direct source
> verification of every critical claim.
>
> **Post-script (same day):** G4.5 slice 1 (`core/ensemble_gate.py`, the pure
> refuted-flip authoriser) landed after this review's snapshot. F2's statement
> that the ensemble gate is "not yet built" is now partially stale: the decision
> algebra exists; the enforcement gap (nothing wired into `persist_verdicts`,
> quarantine still absent at the edge producer) stands. Remediation is tracked
> as V7/V8 in `gap_review_2026-06-11.md`.

**Verdict in one paragraph.** The June reviews worked: every fault-leading code
defect they flagged as "fix now" is fixed, and fixed well (windowed embedding,
dollar-quote injection, LLM timeouts, AGE label indexes with EXPLAIN-verified plan
behavior — better than what the review asked for). The Phase 2–4 code is disciplined
and honest about its seams. The project's serious problems have **moved up a level**:
they are no longer in the code, they are in **what has not been built while the code
raced ahead**. Phase 4's core is done, which makes the validation gate the next
milestone — and *zero* gate assets exist: no planted corpus, no trial harness, no
baseline implementations, no second annotator. Meanwhile two safety invariants the
architecture treats as load-bearing (the provisional-quarantine gate and the §7.2
ensemble gate) are still unenforced *after* the REFUTES creation site shipped — the
documented deferral condition ("no edge-creation site exists yet") has silently
expired. And the remediation tracker itself has gone stale enough to misdirect work.

---

## 1. Status of the June 2026 critical findings (verified against source)

| Prior finding | Status | Evidence |
|---|---|---|
| C1 — silent truncation of long documents | **Fixed** | `core/embeddings.py:35-65` overlapping macro-windows (`_plan_windows`), interior-window pooling at `:171-212`; fail-loud guard retained |
| C2 — `$$` dollar-quote Cypher injection | **Fixed** | `db/age.py:60-84` per-call collision-free dollar tag; attack documented in module docstring |
| C4 — AGE property indexes | **Fixed, exemplary** | Migration `0007`: GIN on `properties` per vertex label + btree on edge endpoints. The migration docstring records that the originally-proposed btree expression indexes *would never be chosen* — real plans use `@>` containment, verified with EXPLAIN. This is exactly the "existence is not use" discipline §6 demands |
| P5/R7 — embedding model identity column | **Fixed** | Migration `0008` adds `model TEXT NOT NULL` to both embedding tables; vector-space identity guard enforced in `candidates.py:332` and ingest |
| LLM call timeouts | **Fixed** | `core/llm.py:32-34, 82-83` — 180 s hard deadline wrapping the retry loop |
| C3/R4 — HNSW/IVFFlat ANN index | **Still open** | No migration creates any ANN index; see F3 below |
| G7/R8 — `provisional` → `provisional_reasons` | **Still open** | `types/nodes.py:106` still `provisional: bool \| None`; see F2 below |
| G1.6/R9 — quarantine gate enforcement | **Still open, condition expired** | See F2 below |
| P1/R10/R11 — out-of-process embeddings, task queue | **Still open** | No queue service in either compose file; embeddings load torch in-process; see F4 below |

---

## 2. Critical findings

### F1. The validation gate is the next milestone and none of its assets exist

Phase 4's core shipped (G4.1–G4.4 done; G4.3 slice 3 on this branch). Per `todo.md`,
the next hard milestone is the validation gate: **"Do not harden any layer or start
Phases 5–7 until the gate passes *and* E1 is go."** What the gate requires vs. what
exists:

- **Planted-contradiction corpus (Trial A0).** Does not exist. The fixture corpus
  (`tests/fixtures/corpus/`, 3 documents) is the loader-development *seed* for A0, not
  A0 — `todo_trials.md` says so itself. No planted SUPPORTS/REFUTES gold edges, no
  dissimilar refuters, no overturning fact, no gold hypothesis states.
- **Trial measurement harness (A1–A6).** No `tests/trials/` or equivalent exists. The
  gate's headline measurements — consistency-vs-verbalized confidence, ensemble-vs-
  single-call contradiction, refuter recall, level-attachment accuracy — have no
  harness, no metrics code, no runbook.
- **E1 baselines (the go/no-go).** Zero code. `todo_trials.md` explicitly warns "the
  go/no-go is only as valid as the strongest baseline" and says to start the baseline
  implementations "before Phase 4 completes." Phase 4 core is complete.
- **Human annotation.** The gate requires level-attachment gold labels from **≥2
  annotators with κ > 0.6**. This is a single-developer project. Who the second
  annotator is, and the calendar time for two labeling passes plus reconciliation, is
  unplanned. This is the longest-lead item on the whole critical path and the only one
  that cannot be parallelized away with more compute.

The June review already flagged the gate sitting structurally late (its G3) and the
plan responded by scheduling A0 "now, parallel with Phase 1 tail." That scheduling
decision was not executed. The result is the exact failure mode the prior review
predicted, one phase later: **every additional slice built before the gate (G4.5,
Phase 5 prep, hardening) deepens the sunk cost that the gate is supposed to be able
to invalidate.** A0 + the trial harness + the E1 baselines are now *the* critical
path; G4.5 is not.

**Action:** stop feature slices after G4.3 lands. Next three increments: (1) A0 corpus
authoring + gold labeling (recruit the second annotator now — this gates everything);
(2) the trial harness with bias-controlled scoring; (3) the E1-lite/E1 baseline rigs.

### F2. Two load-bearing safety gates are unenforced — and their deferral condition has expired

The architecture makes two hard promises about REFUTES, the system's most dangerous
edge type:

1. **Quarantine (§3.1):** a provisional/low-faithfulness proposition "must not drive
   a strong move (e.g., a `REFUTES` that overturns a hypothesis)."
2. **Ensemble gate (§7.2):** "a flip to `refuted` requires the ensemble gate
   (multi-sample LLM + symbolic + temporal agreement), never a single judgment."

Both were legitimately deferred while no code created evidential edges — the
documented condition was "no `REFUTES` creation site until Phase 2/4." **G4.3 slice 3
is that creation site, and it shipped without either gate.** `core/edge_producer.py`
and `core/edge_judge.py` contain no reference to `provisional` or `faithfulness`
filtering (verified by search); a provisional proposition's Fact flows into the judge
and out as a persisted REFUTES edge exactly like a verified one. The ensemble gate is
G4.5 — not yet built — and the symbolic and temporal legs of the ensemble don't exist
at all.

Mitigating context, to be fair to the design: the judge is multi-sample, surfaces
`sign_stable` on the edge rather than smoothing it, and the code docstrings
consistently state that the *refuted state flip* is reserved for the G4.5 gate
(`edge_producer.py:52`, `edge_judge.py:43`). The invariant is intact in prose. But
prose is not enforcement: the moment any consumer (the QBAF adapter is already
shipped) reads SUPPORTS/REFUTES edges into hypothesis state, the §7.2 invariant is
violated by construction, with nothing in the code to stop it.

This also compounds with R8 (`provisional_reasons`): the quarantine gate, when built,
needs to know *why* an atom is provisional (low faithfulness vs unresolved binding vs
budget-deferred re-inference) because the stakes-dependent threshold differs by
reason. Every phase that ships before the bool→set migration makes it more painful;
Facts, Mentions, and evidential edges all exist now.

**Action:** before G4.5 or any hypothesis-state consumer: (a) land R8
(`provisional_reasons`); (b) enforce the quarantine check *in `edge_producer`* — a
provisional-sourced REFUTES is either dropped or persisted with a `quarantined: true`
property that every downstream consumer must filter on; (c) make the QBAF adapter
refuse REFUTES edges that have not passed the (future) ensemble gate, so the
invariant is structural, not conventional.

### F3. Generalize the lesson of F1+F2: deferral conditions have no expiry tracking

Both critical findings above share one root cause, and it is a process defect, not a
code defect. The plan's discipline of "defer eyes-open, document the seam" is good —
but a deferral is recorded as *prose with an implicit trigger* ("until a REFUTES
creation site exists", "start A0 before Phase 4 completes"), and nothing checks the
trigger when the triggering slice ships. Both triggers fired; neither was noticed.
The same mechanism will fire again on the currently-deferred items: G1.11 (box
scoping on indexes — trigger: hybrid retrieval), G1.19 (RRF — same trigger), G3.3
(clingo — trigger: first negation rule), bitemporal as-of indexes (trigger: Phase 5
reader).

**Action:** add a "deferred items and their triggers" table to `HANDOFF.md` (it is
the document every session reads), one line each: item, trigger condition, the G-item
whose landing fires it. Review the table in every slice's PR description.

---

## 3. High-severity findings

### H1. The remediation tracker is stale enough to cause wrong decisions

`gap_review_2026-06.md`'s R1–R13 status table shows essentially everything ☐
unstarted. Verified against code: **R1, R2, R3, R5, R6, and R7 are done** — shipped
under different IDs (G1.13, G1.17, G0.R2, migration `0008`). The damage is concrete:
one of this review's own audit passes, working from the docs, concluded R7 (model
column) was still open while the code-level pass found migration `0008` shipped it.
Any future session or contributor reading the tracker will mis-prioritize the same
way — or worse, re-implement something that exists. The still-genuinely-open subset
is small and important: **R4 (ANN index), R8 (provisional_reasons), R9 (quarantine),
R10 (out-of-process embeddings), R11 (task queue), R12 (supersession semantics), R13
(metrics/auth)**.

**Action:** one reconciliation pass over `gap_review_2026-06.md`: mark R1–R3/R5–R7
done with pointers to the shipping G-items/PRs, leaving the open six unmistakable.

### H2. Ingest and adjudication are still a synchronous, in-process, foreground job

R10/R11 remain open while the workload they were flagged for has *grown*: Phase 4
adds judge calls multiplying as hypotheses × evidence-batches × samples on top of
extraction's samples + verify per proposition. There is still no task queue in either
compose file, no retry/backpressure story, and `EmbeddingSubstrate` still loads
torch into the API process. The first real-corpus ingest (the A0 corpus, ironically)
will be an hours-long unrecoverable foreground run. This was tolerable while testing
single fixtures; the validation gate's own workload is where it stops being tolerable.

**Action:** before running the gate trials: serve embeddings out-of-process (TEI or
vLLM embedding endpoint behind the existing swappable seam) and adopt the simplest
self-hosted queue (the prior review's R11 sketch — `procrastinate` rides the existing
Postgres — remains the right-sized answer). This is gate *infrastructure*, not
hardening, so it does not violate the gate-first ordering.

### H3. ANN indexing (R4) is open, and the in-memory k-NN seam has no owner

The k-NN candidate stage is exact in-memory cosine, and — credit where due — the code
records this eyes-open as "the recall ceiling an approximate pgvector ANN index is
later measured against (G4.6), and the seam where the `<=>` push-down replaces this
loop" (`candidates.py:304-307`). But §5.1 promises the workhorse stage is "sublinear…
reuses the dense index," §6.1's cost model amortizes a "large but **static** reference
corpus" through it, and at that scale loading every vector into Python and doing
O(h×e) cosine breaks both. No migration creates the HNSW index, no todo item owns the
push-down, and no doc records the distance-operator decision (vectors are normalized;
pick `<=>` vs `<#>` once).

**Action:** keep the exact-scan as the G4.6 recall baseline (that's a good design),
but file the owner now: an HNSW migration (with `m`/`ef_construction` recorded), the
`<=>` push-down behind the existing contract, and a measured recall-vs-exact
comparison as part of the gate. Without the owner this becomes the next silently
expired deferral.

### H4. Architecture-doc drift: decisions live in code/HANDOFF that §10 contradicts-by-omission

Two shipped, load-bearing decisions are not in `architecture.md`, which calls itself
"the source of truth for every design decision":

- **The §8↔§9 credibility seam.** G4.3 slice 3 resolved how conditional credibility
  enters the edge pipeline: the judge runs at identity reliability and
  `significance = tier_weight × effective_credibility` (`edge_producer.py:132-144`).
  This is a real design decision — arguably the cleanest reading of §5/§9 — but it is
  recorded only in code docstrings and `HANDOFF.md`. §10's schema section still
  describes `significance` only as "largely inherited from the evidence node's
  source/tier."
- **The G2.4/G2.8 taxonomy-anchor seam.** The §3.1 binding cascade's taxonomy stage
  shipped inside G2.8 slice 2, not G2.4; the gap docs describe this confusingly enough
  that a reader of G2.4 alone would mis-judge what the binder does.

**Action:** backport both into `architecture.md` (§10 edge-properties and §3.1
respectively) and `gap_phase_4_linking_adjudication.md`. Cheap now; expensive after
the next person designs against the prose instead of the code.

---

## 4. Medium-severity findings

- **M1 — Untested infrastructure modules.** No unit tests at all for `db/age.py` (the
  Cypher builder every graph write flows through — its injection defense is only
  covered transitively), `boxes/registry.py`, `domain/loader.py`/`pack.py`,
  `provenance/action_log.py`/`audit.py`, `config.py`. `core/qbaf.py` and
  `core/confidence.py` are exercised only through higher-level paths. The audit-trail
  modules are the sharpest gap: auditability is principle 9, and the code that writes
  the audit record has no direct tests.
- **M2 — Calibration is claimed but has no harness.** Stored edge `strength` is
  specified as "fused, **recalibrated**, expert-correctable" (§10). Fusion exists
  (subjective-logic, G4.3 slice 1); recalibration has no code, no data, and no plan
  item before Phase 7's override-divergence loop. Acceptable pre-gate, but the gate
  itself measures calibration — fold a minimal reliability-diagram harness into the
  A0 trial work, or the gate's calibration criterion is unmeasurable.
- **M3 — No API design.** Two stub endpoints exist; no phase doc lists endpoint
  design (query, search, investigation/Task, override, audit-trail) as an entry
  criterion for Phases 6–7. Same for authn/z: the operations track added in June
  names it, but no concrete item owns "how a viewer's clearance is established."
- **M4 — Deployment runbook absent.** Self-hosting is principle 7, yet nothing
  specifies where the LLM, embedding server, and MinerU run in production, the
  backup/restore drill for what will be the durable record of investigations (a
  `pg_backup` service exists in `compose.prod.yaml`; restore has never been
  exercised), or the domain-pack activation procedure.
- **M5 — Expert UI stack undecided** (Phase 7 entry criterion; fine today, should be
  written down as one).
- **M6 — `torch 2.5.1` pin is aging** (mid-2024 release) — revisit when the
  embedding serving moves out of process (H2), which is the natural moment to drop
  torch from the API image entirely.
- **M7 — PR/branch state vs docs.** HANDOFF says PR #57 awaits review while the
  commit sits on `feat/g4.3-edge-producer`; the docs-vs-git reconciliation in this
  review treats it as shipped. Keep HANDOFF's PR state current — it is the
  session-bootstrap document.

---

## 5. What is in good shape (for calibration)

- **The fix quality on the June criticals is high.** The windowed-embedding fix
  implements the architecture's spec rather than a patch; the migration `0007`
  docstring documenting *why the originally-requested index would never be used*
  (EXPLAIN-verified `@>` containment → GIN, not btree expression) is the single best
  artifact this review encountered — it is the §6 "existence is not use" principle
  practiced, not just stated.
- **The Phase 4 judge implements the §8 disciplines faithfully**: blind judging
  (prompt withholds hypothesis state), per-sample randomized evidence order from a
  deterministic SHA-256 permutation (replayable *and* position-bias-canceling),
  sign/strength separation with `sign_stable` surfaced rather than averaged away,
  prompt/schema SHAs in every Action record.
- **Migrations and CI are disciplined**: 12/12 reversible migrations, ORM-drift
  autogenerate check in CI, integration tests hard-fail (not skip) when the DB is
  missing in CI, per-test DB isolation, the AGE search_path hazard documented and
  pinned at connect time.
- **The todo/gap files do not lie**: no checkbox claimed done without shipped code
  found anywhere in the reconciliation pass. The staleness problem (H1) is confined
  to the *review remediation tracker*, not the phase plans.
- **Candidate generation handles the dissimilar-refuter problem as designed**:
  union (never intersection) of structural and embedding stages, no default
  similarity floor, box-scoped, vector-space identity enforced.

---

## 6. Prioritized actions

| # | Action | Closes | When |
|---|--------|--------|------|
| 1 | Recruit the second annotator; author A0 planted corpus + gold labels | F1 | Now — longest lead item |
| 2 | Build the trial harness (A1–A6 metrics, bias-controlled scoring) | F1, M2 | Now, parallel with #1 |
| 3 | Build E1-lite/E1 baseline rigs (tuned RAG, agentic RAG, expert+search protocol) | F1 | Before any post-G4.3 feature slice |
| 4 | R8 `provisional_reasons` migration + quarantine enforcement in `edge_producer`; QBAF adapter refuses un-gated REFUTES | F2 | Before G4.5 / any state consumer |
| 5 | Reconcile `gap_review_2026-06.md` statuses (R1–R3, R5–R7 → done) | H1 | Now, 30 minutes |
| 6 | Deferred-items trigger table in `HANDOFF.md` | F3 | Now, with #5 |
| 7 | Out-of-process embedding serving + `procrastinate` task queue | H2 | Before running gate trials |
| 8 | HNSW migration + `<=>` push-down owner + recall-vs-exact measurement in gate | H3, R4 | With gate trials |
| 9 | Backport credibility-seam and anchor-seam decisions into `architecture.md` | H4 | With #5 |
| 10 | Unit tests for `db/age.py` and `provenance/*` | M1 | Opportunistic, before Phase 5 |
| 11 | API + authn/z design item as Phase 6 entry criterion; restore drill for `pg_backup` | M3, M4 | Phase 6 entry |

The single sentence that matters: **the code has earned the gate; run the gate before
writing more code that assumes it passes.**
