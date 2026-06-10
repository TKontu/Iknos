# Gap Plan — Phase 1 (Ingest Pipeline)

**Why this file exists.** Phase 1 increments 1–3 (embedding substrate,
segmentation backbone, proposition layer) are built and unit/integration-tested.
The revised plan (`todo_phase_1_ingest.md` + `architecture.md` §3.1, §6.1) adds a
whole **extraction-faithfulness layer** the original Phase 1 did not have, plus
cost/incrementality requirements. This plan lists the code revisions to reach the
revised spec. It folds in (and supersedes) the old `proposition_layer_plan.md`.

**Refs:** §1 (embedding substrate), §2 (segmentation), §3 (proposition layer),
§3.1 (extraction faithfulness), §4 (indexing), §6.1 (cost/incrementality).
Principles 1–3, 6.

## Status (current)

Shipped to `main`: **G1.9** span persistence (#18), **G1.1** structured epistemic
fields + **G1.2** fact/judgement routing (#20), **G1.4** extract-then-verify (NLI) +
**G1.5** faithfulness score (#21). The §3.1 perception-hardening core is in place: a
proposition now carries `polarity/modality/attribution/scope/epistemic_class/routing`
and — when an independent verifier is configured — a derived `faithfulness ∈ [0,1]` and
`provisional` flag, with the verify verdict recorded as its own auditable `Action`.

Remaining (next): **G1.3** multi-sample (the agreement signal that combines into
`faithfulness` via the seam `faithfulness_from_verdict()` left open), **G1.6** quarantine
*enforcement* (the `provisional` flag is now set per node; gating it at edge-creation is
Phase-2-gated), **G1.0** parse front-end (MinerU), then G1.7/G1.8/G1.10–G1.12.

## Current implementation (baseline)

The built proposition layer is sound and retained — its design is captured here
(absorbed from the now-removed `proposition_layer_plan.md`):

- **`core/embeddings.py::EmbeddingSubstrate`** — late chunking (embed once,
  pool spans by offset) + `embed_passages` (batched, for rewritten proposition
  text that is not in the document).
- **`core/segmentation.py::SegmentationBackbone`** — adjacent-window similarity →
  smoothing → depth-score valley detection → O(N) DP segmentation (prefix sums) →
  information-density blend.
- **`core/proposition.py::Propositionizer`** — per-span decontextualization with a
  preceding-**K**-span context window (references resolved from context, claims
  emitted only for the target span); guided JSON decode via `core/llm.py`;
  3-phase run (idempotency filter → semaphore-bounded concurrent inference → serial
  per-span commit); `Proposition` node + `EVIDENCED_BY`→target span; dense
  (`proposition_embeddings`) + lexical-exact (`proposition_lexical_index`,
  `simple` tsvector + GIN) indexes; one `Action` per span (§10.2);
  **Action-based idempotency** keyed on `inputs.target_span`.
- `Proposition` Pydantic model = `{id, text}` only.

**Retained design decisions** (still correct under the revised plan): context-window
decontextualization (O(N) calls / O(N·K) tokens), grammar-constrained decoding,
no long-held transactions, per-span atomic write, lexical-exact `simple` config,
separate `proposition_embeddings` table. The revised plan **adds a faithfulness
layer on top** and **reverses one non-goal** (multi-sample — see G1.3).

## Gaps to close

### G1.0 — Document parse front-end (Stage 0, §1) *(new in revised plan; precedes G1.9)*
The revised §1 adds a **Stage 0** that the original Phase 1 lacked: real case
documents are PDFs/scans (multi-column, tables, figures, OCR-only), not clean text.
Nothing in the codebase parses documents today (no MinerU/parser/PDF/OCR path); the
integration test feeds text directly. This is the new pipeline entry point and gates
ingest of any real document.

- [ ] **Parser behind a fixed contract** (swappable like the LLM): input PDF/scan/doc →
      reading-order text + structure + tables + located figures + formulas +
      per-element `{page, bbox}`. Default impl **MinerU**; Docling/Marker as alternates.
- [ ] **MinerU as a separate hosted service (CLI/HTTP), never vendored** — it is
      AGPL-3.0; the copyleft must stop at the service edge (`config.py` endpoint, like
      the LLM/verifier). See the licensing note in `todo.md`.
- [ ] **`Span.layout {page, bbox}`** — extend `types/nodes.py::Span` (optional `layout`)
      and the span ORM/AGE persistence so a claim resolves to a *region on the page
      image*, not just a character offset. Schema-contract field (§10); see
      `todo_phase_0_foundations.md` Span note. **Wire the write path in G1.9.**
- [ ] **Tables → structured observations:** rows/cells → propositions with column
      semantics preserved, observation-class (§3.1) — not flattened to prose.
- [ ] **Figures located, interpreted later:** store figure region + caption + bbox; a
      Phase-2 vision `extract` operator reads propositions off the figure, provisional.
- [ ] **Parse quality → faithfulness input:** scanned / handwritten / complex-table
      parses marked lower-faithfulness → provisional → triage (feeds G1.5/G1.6); surface
      MinerU's layout visualization for expert QA against the original.

### G1.1 — Structured epistemic fields on `Proposition` (§3.1) *(core)* — ✅ shipped (#20)
Today `Proposition = {id, text}`. `architecture.md` §10 (lines ~771–775) requires
structured, **non-flattened** epistemic fields:

- [x] Extend `types/nodes.py::Proposition` and the extraction contract
      (`core/proposition.py::PropositionExtraction` / `_PropositionOut`) with:
      `polarity` (asserted/negated), `modality` (categorical/probable/possible/
      hypothesized), `attribution` (document/reported-speech/named-source),
      `scope` (quantifier-scope notes), `epistemic_class`
      (observation/testimony/judgement — orthogonal to modality),
      `faithfulness` ∈ [0,1], `provisional` flag. *(faithfulness/provisional landed
      as null placeholders here; computed in G1.4/G1.5.)*
- [x] Update `SYSTEM_PROMPT` + the guided-JSON schema so the model emits these
      fields per proposition; persist them as `Proposition` node properties.
      *(Enum lists interpolated from the StrEnums — no prompt/schema drift; the model
      does not self-report `faithfulness`, per §3.1.)*

### G1.2 — Observations as facts, conclusions as judgements (§3.1, §5) — ✅ shipped (#20)
- [x] Use `epistemic_class` to route: a source's **observations** ingest as facts;
      a source's **conclusions** ingest as defeasible, credibility-weighted
      *judgement-claims*, never as facts (the engine re-derives conclusions). The
      classification + routing flag originate here; the consuming extraction is
      Phase 2, so emit the class and the routing decision now. *(`route_for()` +
      cached `Routing` property on each `Proposition`; consumed in Phase 2.)*

### G1.3 — Multi-sample extraction *(reverses an old non-goal)*
The old `proposition_layer_plan.md` explicitly listed "no multi-sample/calibration
for propositionization" as a non-goal. The revised plan reverses that.

- [ ] Sample the propositionizer N times (reuse §8 calibration machinery): stable
      extractions → high `faithfulness`; unstable → `provisional`/flagged. Feed
      the agreement signal into `faithfulness` (G1.5).

### G1.4 — `verify` step (entailment/NLI) (§3.1) — ✅ shipped (#21)
- [x] Add a `verify` step: check the source span entails the proposition **with
      its polarity and modality**; disagreement sets `provisional`. Prefer an
      **independent verifier — a different model family from the extractor** — to
      cut correlated error (§13). Requires `core/llm.py` to address a second
      model/endpoint (config in `config.py`). *(`core/verify.py::Verifier` reuses
      `LLMClient` against `LLM_VERIFIER_BASE_URL`/`LLM_VERIFIER_MODEL`; one
      proposition per call in the propositionizer's concurrent phase; the verdict is
      recorded as a separate `actor="verifier"` `Action`. Verifier optional —
      absent → faithfulness/provisional stay null, the documented G1.1 mode.)*

### G1.5 — `faithfulness` score, kept distinct (§3.1) — ✅ shipped (#21)
- [x] Record `faithfulness` ∈ [0,1] per proposition, **distinct** from source
      `credibility` (§9, see `gap_phase_0_foundations.md` G0.6) and evidential
      `strength` (§8). Persist on the node. *(`epistemic.faithfulness_from_verdict()`
      — derived from the verify verdict, never self-reported: per-entailment base ×
      multiplicative polarity/modality penalties; sets `provisional` via
      `is_provisional()`. The G1.3 agreement signal combines in via a named seam.)*
- [ ] Wire the faithfulness-gate **metric** (entailment, negation/modality
      preservation accuracy) for **Trial A5** (`todo_trials.md`). *(The decomposed
      verdicts are persisted in `actions.outputs` ready for the metric; computing the
      metric on a labeled corpus is the remaining Trial-A5 work.)*

### G1.6 — Quarantine by stakes (§3.1) *(partial — flag set, enforcement pending)*
- [x] The `provisional` flag is now **set** per proposition (`is_provisional(faithfulness)`,
      G1.5) and persisted on the node.
- [ ] **Enforcement:** provisional / low-faithfulness propositions may exist but
      **cannot drive high-stakes moves** (e.g. a `REFUTES`) until confirmed; route them
      to the expert-triage queue. The rule originates here (the queue UI is Phase 7);
      enforce the gate wherever a proposition feeds an evidential edge. *(Evidential
      edges are Phase 2, so enforcement is gated on that.)*

### G1.7 — Content-addressed cache (§6.1) *(generalizes current idempotency)*
Current idempotency keys on `Action.inputs.target_span` (a span id) — so a
re-segmented or duplicated span re-infers.

- [ ] Replace/extend with a **content-addressed cache** keyed by
      `hash(target_text + context_text + model_version + schema_version)`, so
      unchanged content is never re-inferred across documents or re-segmentation
      ("extract once"). Keep the per-span `Action` for audit.

### G1.8 — Amortize reference processing (§6.1)
- [ ] Reference-corpus / domain-pack boxes are ingested **once** and persisted
      read-only for reuse across investigations; only case documents are processed
      per investigation. Depends on the Phase 0 domain-pack scaffold
      (`gap_phase_0_foundations.md` G0.7) and box tier (`reference`/`schema`).

### G1.9 — Span persistence *(the end-to-end blocker, carried)* — ✅ shipped (#18)
`segmentation.py::segment_document` returns in-memory `(start, end)` tuples;
nothing writes `Span` vertices to AGE or populates the dense span index
(`document_embeddings`). `Propositionizer` assumes spans already exist (the
integration test hand-creates them).

- [x] Persist `Span` vertices + `document_embeddings` rows from the segmentation
      output. *(`core/ingest.py::persist_spans` — deterministic `uuid5` span ids +
      MERGE/upsert + content-hash immutability guard; migration `0005`.)*
- [x] Persist the optional `Span.layout {page, bbox}` (from G1.0) on the same write
      path when the parse front-end supplied it (null when ingesting plain text).
      *(Seam `persist_spans(layouts=...)` in place; populated once G1.0 lands.)*

### G1.10 — Multi-level spans + RAPTOR summaries (§2)
- [ ] Length penalty as the **level knob** → multiple abstraction levels stored as
      `Span` offset ranges with `level` (currently single-level).
- [ ] Coarse levels as **summaries**, not just longer windows (RAPTOR-style upward
      tree) — needed so §5.1 coarse-to-fine pruning has crisp parents.

### G1.11 — `box` on the indexes (cross-phase)
- [ ] Add `box` to `proposition_embeddings` / `proposition_lexical_index` (today:
      `document_id` only) once Phase 2 boxing lands, so retrieval scopes to the
      active working set (§4).

### G1.12 — Multi-span provenance *(optional refinement)*
- [ ] Optionally add `EVIDENCED_BY` to the context spans a proposition drew on for
      reference resolution (today: target span only; context ids live in
      `Action.inputs`).

## Sequencing

0. **G1.0 parse front-end** is the new Stage 0. The `Span.layout` field-add lands
   with it; its write path merges into G1.9 (seam already present). The MinerU
   *service* integration can proceed in parallel (text ingest already works without
   it). *(not started)*
1. ~~**G1.9 span persistence**~~ — ✅ #18.
2. ~~**G1.1 epistemic fields** + **G1.2 routing** (#20) + **G1.4 verify** +
   **G1.5 faithfulness** (#21)~~ — ✅ the §3.1 perception-hardening core.
3. **G1.3 multi-sample** + **G1.6 quarantine enforcement** — calibration + stakes
   gating (G1.6 flag is set; edge-time enforcement gated on Phase 2). ← **next**
4. **G1.7 content-addressed cache** + **G1.8 reference amortization** — cost.
5. **G1.10 multi-level/summaries**, **G1.11 box**, **G1.12 multi-span** —
   incremental, some gated on Phase 2.

## Revised exit criteria (delta over the originals)

- [ ] A real PDF/scan ingests end-to-end **through the parse front-end**: MinerU
      service → reading-order text + tables + figures + `{page, bbox}` → cached
      embeddings → multi-level spans (written to AGE, carrying `layout`) →
      propositions with epistemic fields + faithfulness → dense + sparse indexes,
      span references retained.
- [ ] A claim resolves back to a **region on the original page** (`Span.layout`),
      not just a character offset; table cells ingest as observation-class
      propositions with column semantics.
- [ ] Each proposition carries `polarity/modality/attribution/scope/
      epistemic_class/faithfulness/provisional`; a low-faithfulness proposition is
      quarantined from driving a `REFUTES`.
- [ ] `verify` runs on a different model family from the extractor.
- [ ] Re-ingesting unchanged content hits the **content-addressed** cache (not just
      same-span-id); a static reference corpus is processed once and reused.
- [ ] The faithfulness gate metric is wired for Trial A5.
