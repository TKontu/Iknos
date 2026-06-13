# Phase 5 — Temporal Dynamics & Belief Revision

**Goal:** make knowledge evolve correctly over time — bitemporal validity, non-lossy
supersession, and disciplined re-evaluation when facts arrive, change, or are
deprecated. Layers lifecycle behavior on the Phase 3 propagation mechanism.

**Depends on:** validated core (Phases 3–4). Bitemporal *fields* exist from Phase 0;
this builds the *dynamics*.
**Architecture refs:** §7.3 (belief revision), §7.4 (bitemporal record), §9 (box
deprecation), §12 (propagation), §13 (trigger policy).

**Entry criteria (2026-06-11 architecture assessment).** Phase 5 layers belief
revision on the composed loop, so it does not start until:

- the safety lockdown (R8 → R9 → V7 → V8, `todo_phase_4_*.md`) is merged;
- **W1** (composed-loop orchestrator) and **W2** (synthetic §8 end-to-end fixture)
  are green — Phase 5 must not be the first caller of a loop that has never run;
- **C3 has run** (2026-06-12, `todo_trials.md` Trial C3 Result;
  `docs/trials/c3_age_density_benchmark.md`) — **STAY single-engine** confirmed; the
  Phase-5 query shapes behave exactly as the W9 amendment predicted: edge-property
  filters and supersession-rate writes have **no index path** (the bulk supersession
  update is unindexed — ~10²–10³× the sub-2 ms indexed lookups; **re-measured 2026-06-13 at
  272 ms median / 300 ms p95**, replacing the originally quoted ~1.3 s median, which was a
  contaminated measurement now corrected by the harness's per-rep write isolation — the STAY
  decision is unaffected). **Phase-5 must add an
  edge-property GIN on `SAME_AS.properties` (or a btree on extracted `state`) before
  bitemporal supersession runs at reference-base scale** — fold into the supersession
  work below;
- **W7** (dual-write transaction discipline, `todo.md` *Maintenance backlog*) has
  landed — supersession multiplies multi-statement writes and inherits the
  orphaned-`Action` hazard.

## Bitemporal record (§7.4)

- [ ] Populate `event_time` / `ingested_at` on every claim and evidential edge.
- [ ] **Non-lossy supersession:** a superseding fact sets the predecessor's `valid_to`;
      nothing is deleted. Validity windows queryable ("what did we believe at T").
- [ ] Bitemporal history reachable per node/edge (feeds the Phase 7 audit drill-down).

## Belief revision dynamics (§7.3, §12)

- [ ] On a new/changed/retracted fact, re-evaluate **only** downstream conclusions and
      hypotheses (Layer A delta → Layer B recompute → QBAF re-adjudication of the
      affected sub-region), not the whole graph.
- [ ] Conclusions/hypotheses can be **downgraded or retracted**, not only added
      (non-monotonic).
- [ ] **Decide and implement the re-evaluation trigger policy** (open item, §13):
      eager propagation vs lazy recompute-on-read, and the propagation bound. This
      shapes the Layer A↔B interface.
- [ ] **Split cheap vs expensive re-analysis (§6.1):** symbolic re-propagation (Layer
      A/B, QBAF) on the delta-affected sub-graph runs **freely** (no LLM); expensive LLM
      **re-inference** runs only where **VoI** (§11.1) says it could change the
      conclusion. Beyond the VoI threshold, skip re-analysis.
- [ ] **Budget-bounded mode:** under a fixed budget, spend LLM re-inference in VoI order
      and stop at budget/threshold; flag un-inferred regions provisional, not dropped.
- [ ] **Re-inference monotonicity (§12):** an expensive re-inference runs **at most once
      per evidence-state** of a region (cache key = content + region state hash) and the
      budget strictly decreases — so the VoI↔re-inference loop cannot churn.

## Box lifecycle (§9)

- [ ] Deprecating a box (e.g., a retracted source) flips its `status` and triggers
      belief revision on everything `derived-from` its facts.
- [ ] Promotion pathway (gated, explicit, never automatic): a validated working
      conclusion can change box membership into the reference tier. Define the gate
      check (what must hold before promotion).
- [ ] **Pack/taxonomy versioning (§9.1):** packs are versioned + bitemporal; anchors are
      stamped with the pack version; a pack update is a bitemporal supersession that
      triggers delta-scoped, value-gated belief revision on dependent anchors (re-anchor,
      re-derive levels); removed/merged taxonomy nodes handled like entity merge/split
      (§5.2); broken anchors fall back to induced level + review.
- [ ] **Sensitivity propagation (§9.1):** derived nodes inherit the max (least upper
      bound) of their antecedents' `sensitivity` along provenance; re-propagate on change.
- [ ] **Track-record credibility revision (§9.1):** when a source's claim is refuted,
      lower its credibility for its other claims (belief-revised).

## Exit criteria

- [ ] An overturning fact correctly supersedes its predecessor without data loss, and
      downstream beliefs are re-evaluated locally.
- [ ] "What did we believe at time T" is answerable from validity windows.
- [ ] Deprecating a source box cascades revision; promotion works only through the
      explicit gate.
- [ ] Trigger policy chosen, implemented, and documented.

## Phase risks / decisions

- Re-evaluation can cascade far; the trigger policy + propagation bound must keep it
  tractable (§13).
- Promotion is the contamination hazard — verify tentative case conclusions cannot
  leak into the shared reference base automatically (§9).

## Pipeline-version supersession (§6.1 D4 — merged from `archive/todo_ingest.md` 2026-06-11)

The §6.1 version-change policy lands here (it needs this phase's supersession
machinery; until then the shipped behavior is the loud `StaleExtractionError`):

- [ ] **Contract-compatible upgrade = belief revision, not a cache crisis.**
      Re-extract an affected span as a **new version** — old retained, bitemporal,
      logged (§7.4); run the cheap symbolic re-propagation (Layer A/B) immediately;
      **defer expensive LLM re-derivation behind VoI/budget** (§6.1: at most once per
      evidence-state). Derived nodes carry an `extractor_version` stamp (the only
      cache-related field on graph nodes — the cache itself stays infrastructure,
      not schema).
- [ ] **Deliberate upgrade pattern:** mark old-version entries superseded (cheap
      metadata pass) → prioritized, budgeted **VoI-first backfill** (high-stakes /
      contested spans first). Mixed-version regions are queryable via the stamps and
      surface lower-confidence until backfilled.
- [ ] **Contract-breaking change still raises** and requires explicit migration —
      that path never becomes silent.
- [ ] Exit check (was D4's): bumping the extractor version triggers **defer +
      version-stamp**, not an eager full recompute; the old version is retained and
      the affected region surfaces to triage. (Trial C1 exercises this.)
