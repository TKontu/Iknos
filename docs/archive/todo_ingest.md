# Ingest slice — build plan & architecture bridge

**What this file is.** An implementation-near plan for the *ingest slice* that records the
design decisions resolved in the `epistemic.py` / segmentation / caching thread and
**bridges each to the authoritative architecture section and the existing phase task** it
refines. It does not replace `todo_phase_0_foundations.md` (schema) or
`todo_phase_1_ingest.md` (phase checklist); it sits between the architecture spec and those
checklists, pinning the now-resolved decisions to concrete surfaces.

**Scope.** Parse → embed → segment (multi-level) → propositionize (+epistemic fields) →
verify → `combine_faithfulness` → multi-sample → index, plus the caching/versioning layer
and the schema fields these need.

**Out of scope (handoffs):** cross-document entity resolution (`SAME_AS`) → Phase 2
(`todo_phase_2_graph_construction.md`); reasoning/propagation → Phase 3; **leads /
next-best-move** (a.k.a. proposed actions/inquiries) → runtime (§11.3 / Phase 6–7), *not* an
ingest concern — design resolved this session, recorded in §7 with its single ingest
touchpoint.

---

## 1. Decisions resolved this thread → architecture homes

Each decision, its rule, the section it belongs in, the existing task it refines, and
whether the spec edit is still **pending**.

### D1 — Faithfulness combiner  → §3.1
- **Rule:** `combine_faithfulness(verify, agreement) = verify · calibrate(agreement)`,
  with `agreement = 1.0` as the **identity element** (N=1 / degraded multi-sample reduces
  to verify alone).
- **Why multiplicative:** the two signals are independent failure modes (content-grounding
  vs extraction-stability); multiplication uses both. Weighted blend is *rejected* — at
  `agreement = 1.0` it inflates above `verify`, breaking the reduce-to-today invariant.
  `min` discards the non-binding signal. Over-penalization in the correlated tail is the
  *safe* direction (→ provisional → triage).
- **Calibrate** raw agreement before multiplying (small-N is coarse, e.g. N=3 → {0,⅓,⅔,1});
  use a mild concave / Wilson-style map so it is conservative, not jumpy.
- Refines `todo_phase_1_ingest.md` → "extract-then-verify" + "multi-sample consistency".
- **Pending spec edit:** add the combiner rule to §3.1.

### D2 — Degraded-mode faithfulness (verifier OFF, multi-sample ON)  → §3.1, §10
- **Rule:** `faithfulness = null` (grounding *unassessed*); persist `agreement` in its own
  field. `null ⇒ provisional = true ⇒ triage`. **Never coerce null / absent verify to 1.0.**
- **Three-state faithfulness:** verified-high / verified-low / **unassessed** — the
  "unknown vs low" distinction the rest of the system honors.
- **Why:** high agreement ≠ grounding (a model can *consistently* hallucinate). `agreement`
  is a modifier; `verify` is the assessment. When the verifier later runs, the combiner
  completes cheaply from persisted `agreement` (no re-sampling).
- **Pending spec edits:** §3.1 (the null rule + guardrail); §10 (`faithfulness` nullable
  three-state; new `agreement` field on `Proposition`, distinct from faithfulness).

### D3 — Cache reuse scope  → §6.1
- **Rule:** per-span, version-aware. `key = (span_id, content_hash, extractor_version)`,
  where `extractor_version` folds model + prompt + extraction-schema (+ verifier config,
  since faithfulness depends on it).
- **Cross-doc "extract once" deferred:** decontextualization (§3) makes extraction
  context-dependent, so identical span text ≠ identical proposition; also collides with
  per-occurrence provenance fan-out and per-`Box` governance (sensitivity, credibility).
  Keep `content_hash` as the seam for a future, separately-validated, *context-free
  duplicate-only* optimization.
- Refines `todo_phase_1_ingest.md` → embedding-substrate cache note; aligns with §6.1
  "content-addressed cache · unchanged span ⇒ skip re-extract."
- **Pending spec edit:** state the cache key + deferral in §6.1.

### D4 — Changed-pipeline-version re-run  → §6.1 (xref §9.1, §12, §7.4)
- **Rule (contract-compatible upgrade):** **defer.** Re-extract the span as a *new version*
  (retain old, bitemporal, logged); run **cheap foundedness re-propagation now** (§12);
  **defer expensive LLM re-derivation** behind VoI/budget (≤1 per evidence-state, §6.1);
  surface the affected region to triage. Mixed-version regions are tracked via version
  stamps and shown lower-confidence until backfilled.
- **Deliberate upgrade pattern:** mark old-version entries superseded (cheap metadata pass)
  → prioritized, budgeted **VoI-first backfill** (high-stakes / contested spans first).
- **Rule (contract-breaking):** **raise** + require explicit migration (cached form can't be
  interpreted, or mixing versions is unsafe).
- This is the §9.1 "update-as-belief-revision, version-stamped" pattern applied to the model
  version — no new mechanism.
- **Pending spec edit:** add the version-change policy to §6.1.

### D5 — Multi-level span policy  → §2 (xref §14)
- **Rule:** **configurable, default 2.** Level 0 = current finest (**byte-identical**
  default → zero regression); Level 1 = one coarser pass (reduced length-penalty / larger
  max_len). Policy = ordered list of per-level params `{penalty, max_len, mode:
  segment|raptor}`.
- **Why configurable:** principle 1 ("no single optimal chunk size") makes level count
  empirical — it must be a parameter, not a constant; this also makes the level trials
  (A4/B1) runnable without code changes.
- **Cost:** embed-once (principle 2) ⇒ an extra level is a segmentation + index pass, *not*
  re-embedding → cheap, hence safe to leave as a knob. 3rd level = validated config bump.
- **Guard:** this is the **chunk-text** granularity hierarchy, distinct from the part-whole
  abstraction hierarchy (§14, four distinct hierarchies). A "3 segmentation levels" config
  must not be read as "3 part-whole levels."
- Refines `todo_phase_1_ingest.md` → segmentation-backbone tasks.
- **Pending spec edit:** record default-2 + configurable in §2.

---

## 2. Build order (module-level)

Each stage names its input → output contract; ties to architecture §.

- [ ] **Parse front-end (§1, Stage 0).** PDF/scan/doc → reading-order text + structure +
      tables + located figures + formulas + per-element `{page, bbox}`. MinerU behind a
      fixed contract (Apache-2.0 code; prefer vlm/hybrid backend for permissive model
      weights). Tables → structured observations; figures located here, interpreted later.
- [ ] **Embedding substrate (§1).** Long-context embed **once** per doc; cache contextual
      vectors. All levels/retrieval read these (late chunking).
- [ ] **Segmentation (§2) — D5.** DP over cached vectors; `levels` policy (default 2, L0
      byte-identical). Emit `Span`s at each level with `{page, bbox}` carried through.
- [ ] **Proposition extraction (§3).** Decontextualize → atomic propositions; tag epistemic
      fields (polarity, modality, attribution, scope, `epistemic_class`). Emit mentions +
      in-document `REFERS_TO` binding (cross-doc `SAME_AS` → Phase 2).
- [ ] **Verify (§3.1).** Independent-model NLI entailment → `verify ∈ [0,1]`. May be OFF
      (degraded) → see D2.
- [ ] **Multi-sample (§3.1).** Sample extraction N times → raw agreement → `calibrate()`.
      N=1 → agreement identity 1.0.
- [ ] **`combine_faithfulness` (§3.1) — D1/D2.** `verify · calibrate(agreement)`; null when
      verifier OFF; never coerce to 1.0. Set `provisional` from faithfulness (incl. null) +
      stakes gate. Keep the three confidence types separate.
- [ ] **Cache layer (§6.1) — D3/D4.** Content-addressed `(span_id, content_hash,
      extractor_version)`; version-change → defer + version-stamp + lazy cascade.
- [ ] **Index (§4).** Dense (pgvector) + sparse (BM25), box-scoped.

---

## 3. Schema touchpoints (what this slice needs from Phase 0 / §10)

- [ ] `Span.layout {page, bbox}` — optional, for visual provenance. *(already in Phase 0 /
      §10)*
- [ ] `Proposition.faithfulness` — **nullable, three-state** (verified-high / verified-low /
      unassessed). **New: do not store as non-null float.**
- [ ] `Proposition.agreement` (a.k.a. `extraction_agreement`) — **new field**, persisted
      separately from faithfulness; calibrated multi-sample consistency.
- [ ] `Proposition.provisional` — set from faithfulness (incl. null) + stakes. *(exists)*
- [ ] Credibility stays **derived, not stored** (`reliability_prior` + `source_interest` on
      `Box`; `interest_alignment` derived). *(already resolved)*
- [ ] Cache is **infrastructure, not graph schema** — keys/entries live outside the property
      graph; only the `extractor_version` stamp lands on derived nodes (for D4 tracking).

---

## 4. Proposed module surface

Light, indicative — not binding.

- `parse/` — MinerU adapter behind the fixed contract.
- `segment.py` — DP multi-level segmentation; reads `levels` policy (D5).
- `propositions.py` — decontextualization + epistemic-field tagging.
- `verify.py` — independent-model NLI; togglable (degraded mode).
- `epistemic.py` — **single source** for `combine_faithfulness(verify, agreement)` (D1/D2),
  `calibrate(agreement)`, the three-state faithfulness type, and the provisional gate.
- `cache.py` — content-addressed store; key construction + version-change policy (D3/D4).
- `ingest.py` — orchestrator wiring the stages; emits to the graph + indexes.

---

## 5. Bridge actions — pending architecture.md edits (sync spec to decisions)

These keep the spec authoritative. Recommended batch:

- [ ] **§3.1** — record D1 (multiplicative combiner, blend rejected, calibration) and D2
      (degraded-mode null, three-state, never-coerce-to-1.0 guardrail).
- [ ] **§10** — `faithfulness` nullable three-state; add `agreement` field on `Proposition`.
- [ ] **§6.1** — record D3 (cache key + cross-doc deferral) and D4 (version-change: defer /
      version-stamp / lazy VoI-gated cascade; raise on contract-break).
- [ ] **§2** — record D5 (configurable level policy, default 2, L0 byte-identical;
      chunk-text vs part-whole guard).
- [ ] Cross-check no stale prose contradicts the above (esp. §6.1 caching paragraph, §3.1
      multi-sample sentence).

## 6. Validation & exit criteria

- [ ] Re-ingesting an unchanged document is a full cache hit (D3) — no re-extraction.
- [ ] Bumping `extractor_version` invalidates correctly and triggers **defer**, not eager
      full recompute (D4); old version retained, region surfaced.
- [ ] `combine_faithfulness` is the single code path; `agreement = 1.0 ⇒ faithfulness =
      verify` exactly (D1); verifier OFF ⇒ `faithfulness = null`, never 1.0 (D2).
- [ ] `levels` policy is a parameter; default produces 2 levels with L0 byte-identical to
      the pre-multi-level finest (D5).
- **Trials that exercise this slice:** A5 (extraction faithfulness incl. epistemic_class),
  A4 (part-whole/level — sweeps the `levels` policy), C1 (re-eval trigger + incremental
  cost + cache behavior, exercises D3/D4).

## 7. Related decision — leads / next-best-move (runtime, **not** ingest)

Resolved this session (critique adopted). Recorded here for the ledger; **its home is §11.3
+ Phase 6–7, not the ingest slice.** Summary so the decision isn't lost:

- **Not a new node category.** Internal computational *moves* (corroborate,
  find-contradiction, deduce, expand, retrieve) = an ephemeral, derived projection over the
  open-trace set — no persistence beyond the log. External *inquiries* (acquire a doc,
  commission a test, interview) = a **sub-type of `Task`** (`kind = inquiry/acquire`),
  reusing `DECOMPOSES_INTO` + `answer_state`. No epistemic-graph node; the intentional ↔
  epistemic firewall holds (no truth-state, no `EVIDENCED_BY`, no credibility on leads).
- **Naming:** avoid `Action` (taken by the §10.1 process log). Use *lead* (surfaced list),
  *move* (internal), *inquiry* (persisted external sub-Task).
- **Ranking:** extend §11.1 VoI to **VoI-per-unit-cost** (EVSI under budget); one signal,
  three consumers (machine re-inference budget, review queue, next-best-move list). For
  external inquiries, cost is expert-/pack-supplied, not computed.
- **Inert until accepted:** a `proposed` lead is advisory and must **not** drive retrieval /
  candidate scope until accepted — else the value-gate explosion relocates into the
  reasoning scope (extends §11.2's decomposition caution).
- **Calibration:** log predicted-vs-realized VoI per move-*type*, fed back like the §10.3
  reconciliation loop; a slow per-type prior, credited only on proximate cause.
- **Type-conditioned generator:** `Task.type` shapes it (normative → "obtain the governing
  standard"; causal → "find a refuter") — consistent with §11.2 mode selection.
- **Remediation** is the *answer to a normative Task*, not a lead — decision-support, never
  auto-executed, never re-injected as evidence.

**Ingest touchpoint (the only one):** an external inquiry's **outcome re-enters here** — an
acquired document or test report is a new source that flows through *this* ingest pipeline
at the loop's "revise" step. No special ingest change is needed: re-ingest is already
covered by D3 (cache key) and D4 (version handling). The lead/inquiry layer closes its loop
*through* ingest, which is why it's cross-referenced rather than specified here.

*Spec edit (non-ingest, tracked separately from §5):* author §11.3 + the `Task` sub-type
note in §10 + the Phase 6–7 touchpoints.
