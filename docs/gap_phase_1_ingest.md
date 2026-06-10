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

**G1.3** multi-sample extraction (#23) closed the consistency half: the extractor is sampled
N times, equivalent extractions are clustered, and the cross-sample `agreement` folds into
`faithfulness` multiplicatively (`combine_faithfulness`). Default `LLM_EXTRACT_SAMPLES=1` keeps
it a strict no-op until enabled.

**G1.7** content-addressed cache (#25) shipped its core: extraction idempotency is now
version-aware — keyed on `(span_id, content_hash)` over the full extractor pipeline, so a changed
model/prompt/regime/verifier re-extracts (or fails loud) instead of serving a stale extraction.

**G1.0/G1.0b** parse front-end shipped: the contract, null parser, `Span.layout` wiring, and
the **MinerU HTTP client** + bytes-in entry point are in; only standing up the live MinerU
service + table/figure interpretation (Phase 2) remain. Remaining (next): **G1.6** quarantine
*enforcement* (the `provisional` flag is now set per node; gating it at edge-creation is
Phase-2-gated), then G1.7b (cross-doc reuse)/G1.8/G1.10–G1.12.

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

### G1.0 — Document parse front-end (Stage 0, §1) *(new in revised plan; precedes G1.9)* — 🟡 contract + MinerU client shipped (G1.0/G1.0b)
The revised §1 adds a **Stage 0** that the original Phase 1 lacked: real case
documents are PDFs/scans (multi-column, tables, figures, OCR-only), not clean text.
This is the new pipeline entry point and gates ingest of any real document.

**Shipped (G1.0 contract slice):** the parse **contract** + the identity **null parser** +
the wiring that threads parser output onto `Span.layout` through G1.9's
`persist_spans(layouts=...)` seam, plus a `parse` provenance Action and parse-identity
folded into the segmentation hash. Plain-text ingest is a first-class Stage-0 mode
(layout `None`), unchanged in behaviour.
**Shipped (G1.0b MinerU client):** `core/mineru.py::MinerUParser` — an httpx `Parser` over
our **own versioned text+offsets wire schema** (a service-side adapter maps MinerU → it, so
the AGPL coupling stays on the service edge), validated in two fail-loud gates (pydantic
envelope + `ParseResult.from_offsets`, which slices element text from the blob and rejects
out-of-range / overlapping / out-of-order offsets and dropped text); retries transport/5xx
only; `parser_version` comes from the service so an upgrade auto-invalidates the parse hash.
Plus the bytes-in `ingest.ingest_document_bytes` entry point (parse hash keyed on the bytes
digest), `make_parser` factory, `NullParser.parse`, and `PARSER_TIMEOUT_S`.
**Open:** standing up the actual MinerU **service** (ops/AGPL-side adapter that emits the
wire schema) and table/figure interpretation (Phase 2).

- [x] **Parser behind a fixed contract** (swappable like the LLM): `core/parse.py`
      (`ParseElement`/`ParseResult`/`Parser` protocol). Reading-order `text` and per-element
      char ranges are *derived* (offset drift impossible); `{page, bbox}` geometry carried
      per element. `ParseResult.from_offsets` (G1.0b) is the real-parser entry — slices
      element text from the parser's blob at supplied offsets, validating the tiling.
- [x] **MinerU as a separate hosted service (HTTP), never vendored** — it is
      AGPL-3.0; the copyleft stops at the service edge. *(G1.0b: `MinerUParser` client +
      versioned wire contract shipped behind `config.parser_base_url` / `parser_kind`, empty
      ⇒ null parser. Remaining: stand up the service that speaks the wire schema.)*
- [x] **`Span.layout {page, bbox}`** — `types/nodes.py::Span.layout` (G1.9) is now **fed**
      by `parse.layouts_for_spans` through `persist_spans(layouts=...)`. The persisted dict
      is versioned + **multi-region** (a span straddling a column/page break carries several
      regions), each region with `origin`/`page_size`/`unit` (a bbox is unrenderable without
      them). Null parser ⇒ `None`.
- [ ] **Tables → structured observations:** rows/cells → propositions with column
      semantics preserved, observation-class (§3.1) — not flattened to prose. *(Phase 2;
      `ParseKind.TABLE` reserved.)*
- [ ] **Figures located, interpreted later:** store figure region + caption + bbox; a
      Phase-2 vision `extract` operator reads propositions off the figure, provisional.
      *(Phase 2; `ParseKind.FIGURE`/`CAPTION` reserved.)*
- [ ] **Parse quality → faithfulness input:** scanned / handwritten / complex-table
      parses marked lower-faithfulness → provisional → triage (feeds G1.5/G1.6); surface
      MinerU's layout visualization for expert QA against the original. *(`SourceQuality`
      carried per element/region now; **consumed** in G1.5/G1.6.)*

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

### G1.3 — Multi-sample extraction *(reverses an old non-goal)* — ✅ shipped (#23)
The old `proposition_layer_plan.md` explicitly listed "no multi-sample/calibration
for propositionization" as a non-goal. The revised plan reverses that.

- [x] Sample the propositionizer N times: stable extractions → high `faithfulness`;
      unstable → `provisional`/flagged. Feed the agreement signal into `faithfulness`.
      *(`core/consistency.py` — deterministic greedy-vs-representative clustering, `agreement`
      = distinct-sample fraction, medoid canonical. `epistemic.combine_faithfulness(verify,
      agreement) = verify × agreement` realizes the seam — multiplicative, so a verified-but-
      unstable proposition is quarantined. Per-sample fan-out under the same semaphore as the
      verifier; `agreement` persists on the node and the extract `Action` audits `n_samples` +
      per-prop agreement for Trial A5. Config `LLM_EXTRACT_SAMPLES` (default 1 = no-op) /
      `PROP_AGREEMENT_THRESHOLD`. Degraded mode — verifier off, N>1 — persists `agreement` but
      leaves `faithfulness`/`provisional` null. No migration — schemaless AGE props.)*

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
      `is_provisional()`. The G1.3 agreement signal now combines in via
      `combine_faithfulness()` (#23).)*
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

### G1.7 — Content-addressed cache (§6.1) *(generalizes current idempotency)* — ✅ core shipped (#25)
Pre-G1.7 idempotency keyed on `Action.inputs.target_span` (a span id) alone — so a span was
skipped forever even after the **extractor model / prompt / sampling regime / verifier** changed,
silently serving a stale extraction (the production-correctness bug).

- [x] **Version-aware, per-span key.** Idempotency now keys on `(span_id, content_hash)`, where
      `content_hash` = `sha256(target_text + context_text + model + EXTRACT_SCHEMA_VERSION +
      sampling[incl. n_samples] + verifier_sig)` — `core/cache.py::extraction_content_hash` (pure,
      mirrors `ingest.span_content_hash`). Same span + same pipeline → true no-op; **changed**
      pipeline → loud `StaleExtractionError` (mirrors `DocumentResegmentationError`; cascade
      re-extract deferred — G1.7b), so a model upgrade can never serve a stale extraction. The hash
      + `schema_version` persist on the extract `Action.inputs` (per-span Action kept for audit);
      `EXTRACT_SCHEMA_VERSION`/`VERIFY_SCHEMA_VERSION` are manually-bumped contract versions. A
      partial functional index on `actions((inputs->>'target_span'), timestamp DESC)` keeps the
      lookup O(log n) (migration `0006`).
- [ ] **G1.7b — cross-document "extract once" reuse.** Reuse the extraction *output* across
      documents / re-segmentation (identical text anywhere skips the LLM and replays cached
      propositions into the new span) — needs a content-addressed output store + replay +
      verify/faithfulness cache design. Soundness note: this is why the shipped key is per-span,
      **not** purely content (a pure-content skip would drop a second span carrying identical text).
- [ ] Cascade re-extraction: on a stale span, purge its old propositions/edges/index rows and
      recreate (pairs with the resegmentation-cascade deferral in `ingest.py`).

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

0. ~~**G1.0/G1.0b parse front-end**~~ — ✅ the new Stage 0: contract + null parser +
   `Span.layout` write path (G1.0), then the **MinerU HTTP client** + `from_offsets`
   validated slicer + bytes-in `ingest_document_bytes` (G1.0b). Standing up the live
   MinerU *service* (it speaks our wire schema) + table/figure interpretation (Phase 2)
   are the only remainders.
1. ~~**G1.9 span persistence**~~ — ✅ #18.
2. ~~**G1.1 epistemic fields** + **G1.2 routing** (#20) + **G1.4 verify** +
   **G1.5 faithfulness** (#21) + **G1.3 multi-sample** (#23)~~ — ✅ the §3.1
   perception-hardening core (consistency *and* verification).
3. ~~**G1.7 content-addressed cache** (core)~~ — ✅ #25: version-aware per-span
   idempotency (`core/cache.py`, migration `0006`). Cross-doc reuse is G1.7b.
4. **G1.6 quarantine enforcement** — stakes gating (G1.6 flag is set per node;
   edge-time enforcement gated on Phase 2). ← **next**
5. **G1.7b cross-doc reuse** + **G1.8 reference amortization** — remaining cost work.
6. **G1.10 multi-level/summaries**, **G1.11 box**, **G1.12 multi-span** —
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
      same-span-id); a static reference corpus is processed once and reused. *(Partial,
      #25: re-ingest of unchanged content is a true no-op and a changed pipeline correctly
      re-extracts — keyed on `(span_id, content_hash)`. Cross-content reuse "not just
      same-span-id" is G1.7b; reference-corpus amortization is G1.8.)*
- [ ] The faithfulness gate metric is wired for Trial A5.
