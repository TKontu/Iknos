# Phase 5 — Temporal Dynamics & Belief Revision

**Goal:** make knowledge evolve correctly over time — bitemporal validity, non-lossy
supersession, and disciplined re-evaluation when facts arrive, change, or are
deprecated. Layers lifecycle behavior on the Phase 3 propagation mechanism.

**Depends on:** validated core (Phases 3–4). Bitemporal *fields* exist from Phase 0;
this builds the *dynamics*.
**Architecture refs:** §7.3 (belief revision), §7.4 (bitemporal record), §9 (box
deprecation), §12 (propagation), §13 (trigger policy).

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

## Box lifecycle (§9)

- [ ] Deprecating a box (e.g., a retracted source) flips its `status` and triggers
      belief revision on everything `derived-from` its facts.
- [ ] Promotion pathway (gated, explicit, never automatic): a validated working
      conclusion can change box membership into the reference tier. Define the gate
      check (what must hold before promotion).

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
