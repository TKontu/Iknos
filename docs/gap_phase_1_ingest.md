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

**2026-06 review** (`review_2026-06_architecture_plan.md`) added **G1.13–G1.19**: two
critical correctness fixes (G1.13 long-document truncation, G1.14 polarity-blind
agreement clustering — both currently silent-wrong on real inputs), two
silent-staleness closures (G1.15 prompt-hash cache key, G1.16 embedding-model
identity), a robustness batch (G1.17), the structured-table contract slot (G1.18,
time-sensitive while the wire schema is on a branch), and the RRF fusion decision
(G1.19). **G1.13 slice 1 + G1.14 are now shipped** — the two cheap critical fixes that
stop silent data corruption (over-long-document refusal; polarity-aware agreement with
twin quarantine), landed before any further perception-layer tuning and before Trial A5
fits thresholds. **G1.15 (prompt/schema-hash cache key) + G1.16 (embedding-model identity
column + ingest guards + `reembed` reindex path) are now shipped** — the two silent-staleness
closures; G1.15 triggers one loud full re-extraction on first deploy. **G1.18 (structured
table payload in the parse wire contract) is now shipped** — a `TABLE` element carries a
validated `Table`/`TableCell` grid through the wire schema, with element-relative cell
offsets rebased to document-absolute at persistence; the 2-D structure now survives Stage 0
(consumer stays Phase 2). **G1.13 slice 2 (windowed embedding) is now shipped** — long
documents are embedded in overlapping macro-windows (each span pooled from the window where
it is furthest from an edge), so they ingest with full dense coverage instead of slice 1's
fail-loud refusal (which is now removed); the windowing policy folds into `span_content_hash`
and the window layout is recorded on the segment Action. **G1.17 robustness hardening (R1–R7)
is now shipped** — one batch: per-span error isolation + a `PropositionizeReport`,
verifier-failure degradation, `pool_span`→`None` (zero-vector sentinel removed), parser/
segmenter `actions` indexes (migration 0010), a per-LLM-call deadline, `EmbeddingSubstrate`
lifecycle, and `cypher_map` property fuzzing. Next: **G1.6 quarantine enforcement** stays
Phase-2-gated (no SUPPORTS/REFUTES creation site to gate yet), so the remaining Phase-1 work
is **G1.7b cross-doc reuse / G1.8 reference amortization** and **G1.10 multi-level/RAPTOR**.

**Fixture corpus (exit-criterion seed) is now shipped** — `tests/fixtures/corpus/`: three
real documents + a `manifest.toml` of regression anchors, a typed model-free/DB-free loader
(`tests/fixtures/corpus.py`), and `tests/unit/test_corpus.py`. It carries the two required
anchors — a > 8200-word document (G1.13: `tokens ≥ words > MAX_MODEL_TOKENS`, so multi-window
is CI-provable without the model) and an `"ambiguous"`-polarity waver span (G1.14) — plus
observation/judgement routing anchors (G1.2). Anchors store quotes, not offsets (located +
uniqueness-checked at load). This is the labelled input Trial A5 (faithfulness-gate metric)
consumes.

**Re-assessment after Phase 2/3 merged to `main`** (G2.1–G2.7 boxes + reference binding +
credibility + part-whole + provenance; G3.4–G3.9 reasoning core). Three items previously
called "Phase-2-gated" were re-checked against the merged code:
- **G1.6 quarantine enforcement** — still gated: no `SUPPORTS`/`REFUTES` creation site
  exists yet (`composed_loop.py` documents the evidential layer as Phase-4 work). Land the
  `is_provisional` gate where evidential edges are first created.
- **G1.19 RRF fusion** — still gated: no hybrid-retrieval consumer queries both indexes yet
  (Phase-4 candidate generation). Nothing to fuse into.
- **G1.11 box on indexes** — partially unblocked (case boxes exist via G2.1), but propositions
  are indexed in Phase 1 *before* any box is assigned at Phase-2 extract time; threading a box
  through ingest needs an architectural decision, so it is sequenced after the un-gated work.
Genuinely actionable un-gated Phase-1 work now: **G1.10**, **G1.7b**, **G1.8**, **G1.12**.

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
- [x] **Part A — multi-level offset spans (shipped).** Length penalty as the **level
      knob** → multiple abstraction levels stored as `Span` offset ranges with `level`.
      `core/segmentation.py`: `SegmentLevel` (frozen) + `default_level_policy()` (default
      2 levels — fine + one coarse; the count is the list length, configured not coded)
      + `SegmentationBackbone(levels=…)` + `segment_document_levels`, which pools/derives
      the boundary signal **once** and runs the DP per level (embed once — §1/§2; only
      penalty/`max_len` differ). `core/ingest.py`: `_ingest_parsed` persists every level
      from the one embedding pass; `_segmented_hash` is per-`(document, level)` and each
      level carries its own content hash + segment `Action`, so coarse levels are **purely
      additive** (level 0 byte-identical; no forced resegmentation on deploy).
      `SpanPersistResult.coarse` carries the coarse results; the finest level feeds the
      proposition layer. Levels are independent granularities — strict containment/parent
      links are Part B / Phase-2 `PART_OF` (G2.5). Tests: `test_segmentation.py`
      (multi-level pure logic — policy, per-level params byte-identity, coarse-merges,
      level-0 ↔ single-level agreement, degenerate/empty) + `test_ingest_layout.py`
      (live-AGE multi-level persistence + per-level idempotency). *No production ingest
      entrypoint constructs the segmenter yet (tests inject it); wire it to
      `default_level_policy()` when that entrypoint lands.*
- [ ] **Part B — RAPTOR summaries (deferred).** Coarse levels as **summaries**, not just
      longer windows (RAPTOR-style upward tree) — needed so §5.1 coarse-to-fine pruning
      has crisp parents. Adds ingest-time LLM cost; gated on the §2 "confirm it's worth
      the pruning benefit before scaling" decision. Next increment after Part A.

### G1.11 — `box` on the indexes (cross-phase)
- [ ] Add `box` to `proposition_embeddings` / `proposition_lexical_index` (today:
      `document_id` only) once Phase 2 boxing lands, so retrieval scopes to the
      active working set (§4).

### G1.12 — Multi-span provenance *(optional refinement)*
- [ ] Optionally add `EVIDENCED_BY` to the context spans a proposition drew on for
      reference resolution (today: target span only; context ids live in
      `Action.inputs`).

---

*G1.13–G1.19 originate in the 2026-06 architecture/code review
(`review_2026-06_architecture_plan.md`); each entry names its review finding.*

### G1.13 — Long-document coverage: truncation guard, then windowed embedding (§1) *(review C1 — **critical**, silent data loss)*

**Why.** `EmbeddingSubstrate.embed_document` (`core/embeddings.py`) tokenizes with
`truncation=True, max_length=8192` and returns no signal that truncation happened.
For any document past ~8k tokens (≈12–20 PDF pages — i.e. *most real case
documents*): `pool_span` finds no overlapping tokens for spans beyond the cutoff and
returns a zero vector; `persist_spans` skips those dense rows; the content is
**silently invisible** to dense retrieval and the §5.1 candidate funnel — the exact
"silent false negative" §5.1 warns about. Segmentation similarity past the cutoff is
likewise undefined. Two slices, shippable independently:

- [x] **Slice 1 — fail-loud guard (do first; tiny).** *Superseded by Slice 2 and
      removed.* The stopgap tokenized **without** truncation and raised
      `DocumentTooLongError` (pure `_raise_if_truncated`) when the true token count
      exceeded `MAX_MODEL_TOKENS` — turning silent data loss into a loud refusal "until
      Slice 2 lands". Slice 2 now covers any length, so the error class and guard are
      gone; their guarantee (no span silently dropped past the cutoff) is upheld by full
      windowed coverage.
- [x] **Slice 2 — overlapping macro-windows ("late chunking over windows").**
      `embed_document` tokenizes the whole document once **without truncation**
      (content tokens only) and tiles it into overlapping windows (`_plan_windows`,
      fixed overlap `WINDOW_OVERLAP_TOKENS` = 1024 — a **constant, not config**), one
      model forward pass per window, each re-framed with the model's own special tokens
      (so interior windows are bracketed). `DocumentContext` holds a list of windows,
      each with its own `token_embeddings` + char-offset mapping;
      `pool_span(start_char, end_char)` selects the window where the span sits
      **furthest from a window edge** (maximizes `min(start−win_start, win_end−end)`
      among windows containing the span's tokens) and pools there — never averaged
      across windows. Callers (`ingest.py`, `segmentation.py`) keep their current API.
- [x] **Provenance + idempotency:** the window layout (count, boundaries, overlap,
      model max, window token size) is recorded on the segment `Action.inputs`
      (`window_layout`), and the windowing **policy** (overlap / model max / window
      size — `DocumentContext.windowing_policy`, not the data-dependent boundaries)
      folds into `span_content_hash`, so a changed windowing policy re-segments instead
      of silently reusing spans pooled under the old policy. One-time loud
      resegmentation on first deploy, like G1.15.
- [x] **Segmentation across windows:** realized through `pool_span`'s per-span
      interior-window selection rather than a separate code path — each sentence pools
      from its most-interior window, so two adjacent sentences (tiny relative to the
      1024-token overlap) select the *same* window and their cosine compares embeddings
      from one consistent context. A document that fits one window is byte-identical to
      the pre-windowing path, so boundary placement is unchanged; `segmentation.py` is
      untouched but for a comment documenting this transparency.
- [x] **Tests (pure, hand-built windows):** `_plan_windows` covers single-window /
      overlap-coverage / anchored-final-window / `overlap≥size` rejection; multi-window
      `pool_span` selects the interior window for an overlap-zone span and the sole
      window for an edge span; every span across >2 windows pools to a non-zero vector;
      a no-token span returns the zero-vector fallback; `window_layout`/`windowing_policy`
      shape; `span_content_hash` moves on a windowing-policy change. The model-backed
      byte-identical / no-op-twice properties hold by construction (single window = n=1
      case, pool math unchanged) and are exercised by the existing single-window
      `pool_span` tests. *(`test_embeddings.py`, `test_ingest.py`.)*

### G1.14 — Polarity-aware agreement clustering + degenerate-sampling guard (§3.1) *(review C2 — **critical**, inflated confidence on negation flips)*

**Why.** `core/consistency.py::cluster_candidates` forms clusters by embedding
cosine alone (`threshold 0.86`). Sentence embeddings place a claim and its negation
nearly on top of each other (typically cosine > 0.9), so asserted and negated
variants of the same claim co-cluster: 3-assert/2-negate across 5 samples yields
**agreement 1.0** — maximum confidence on precisely the polarity instability §3.1
exists to catch — and `canonical_of` makes the persisted polarity a sample-
distribution coin flip. The `Candidate` dataclass already carries the fields; they
are just unused for identity.

- [x] **Hard-partition before clustering:** `consistency.cluster_candidates_partitioned`
      groups candidates by identical `(polarity, epistemic_class)` and runs the untouched
      greedy-against-representative `cluster_candidates` *within* each group. Modality stays
      soft; polarity and epistemic class are identity. Deterministic group order (sorted).
- [x] **Cross-polarity instability is a negative signal, not noise:**
      `consistency.consolidate_samples` detects **polarity twins** — two clusters of opposite
      polarity whose medoids' cosine ≥ threshold. Each side keeps its own distinct-sample
      `agreement` (3-assert/2-negate → 0.6 / 0.4, never 1.0), both canonical propositions are
      set `provisional` (OR-folded in the verify pass so a faithful verdict cannot clear it),
      and the twin pairing is recorded in the extract `Action.outputs` (`polarity_twins`) for
      Trial A5.
- [x] **Degenerate-sampling guard (review P4):** `Propositionizer.__init__` already fails
      loud when `n_samples > 1` under a greedy (temperature 0, no top_p) regime — retained,
      tested (`test_multi_sample_rejects_greedy_sampling`).
- [x] **Tests (pure, hand-built vectors):** asserted+negated near-identical candidates land
      in separate clusters (agreements sum ≤ 1); twin detection sets `provisional` on both and
      surfaces the pair; same-polarity candidates cluster as before; distinct opposite-polarity
      claims are not twinned; the temperature guard fires. (`test_consistency.py`,
      `test_proposition.py`.)
- [x] **Ordering note for Trial A5:** landed **before** A5 fits `PROP_AGREEMENT_THRESHOLD`,
      so the threshold is calibrated against the polarity-aware clusterer.

### G1.15 — Cache key: hash the actual prompt + schema, not a hand-bumped constant (§6.1) *(review A4)* — ✅ shipped

**Why.** `extraction_content_hash` (`core/cache.py`) discriminates on
`EXTRACT_SCHEMA_VERSION`, a constant whose docstring says "bumped on any prompt /
schema / enum change" — a human-discipline guard on exactly the staleness class
G1.7 was built to close. Edit the prompt, forget the bump, and every cached span
silently replays the old extraction.

- [x] Add to the hash payload: `prompt_sha` = SHA-256 of the rendered prompt
      scaffold and `schema_sha` = SHA-256 of the canonical JSON (`sort_keys=True`,
      compact separators) of the guided-decoding schema. *(Shipped: two pure leaf
      helpers `cache.sha256_hex`/`canonical_json_sha256`; `proposition.extractor_prompt_sha`
      renders `build_messages` with sentinels — covering `SYSTEM_PROMPT` **and** the
      CONTEXT/TARGET wrapper, per-span text excluded — and `extractor_schema_sha` over
      `EXTRACTION_SCHEMA`; the verifier signature carries `Verifier.prompt_sha`/`schema_sha`
      (verifier `SYSTEM_PROMPT` + `VERIFY_SCHEMA`).)*
- [x] Keep `schema_version` in the key as a *semantic* version of the output shape;
      it no longer carries invalidation alone.
- [x] **Tests:** `prompt_sha`/`schema_sha` each independently move the cache key; a
      one-char prompt edit moves `extractor_prompt_sha`; re-ordering schema keys does
      not (`canonical_json_sha256`); toggling/rewording the verifier still does.
- [ ] **Expected effect (operational, on first deploy):** one-time full re-extraction
      on the next run after this lands (the key changes). That is correct and loud, not
      a regression.

### G1.16 — Embedding-model identity on dense rows + reindex path (§4) *(review A5)* — ✅ shipped

**Why.** `document_embeddings` / `proposition_embeddings` rows carry no record of
which model produced them. Swap or upgrade the embedding model and the ANN index
becomes a mixed-space soup — cosine across spaces is meaningless — and *nothing can
even detect* the condition.

- [x] **Migration:** add `model TEXT NOT NULL` to both tables; backfill existing
      rows with `'BAAI/bge-m3'`; record `model` as the vector-space identifier in a
      column comment. *(Migration `0008`: add NOT NULL with a `server_default` to
      backfill atomically, then drop the default so future inserts must name their
      model. Mirrored in `db/orm.py` for the autogenerate-drift gate.)* (Dimension is
      implicit in the pgvector column; a model change that alters dimension fails loudly
      already — same-dimension swaps are the silent case this closes.)
- [x] **Ingest guard** (mirror `DocumentResegmentationError`): before upserting,
      if rows exist for this document (or proposition set) under a *different*
      `model`, raise `EmbeddingModelMismatchError` — never mix spaces in place.
      *(`core/embeddings.py::EmbeddingModelMismatchError`; checked in
      `ingest.persist_spans` (span rows) and `proposition._guard_embedding_model`
      (proposition rows — the load-bearing case, since the extraction cache key keys on
      the *LLM* model, not the embedding model, so a substrate swap slips past
      `StaleExtractionError`).)*
- [x] **Reindex path:** `scripts/reembed.py` (CLI) over `core/reembed.py` (logic) —
      for a target model: re-run `embed_document`+`pool_span` over each document's raw
      text to refresh span vectors, and `embed_passages` over proposition texts (read
      back from AGE); batched, idempotent (skip rows already on the target model),
      commits per batch (durable/resumable). Substrate injected so it is testable
      without a model download.
- [x] **Tests:** both mismatch guards raise (span + proposition, integration); reembed
      converges to all-rows-on-target-model and a second pass is a 0/0 no-op
      (`test_embedding_model_identity.py`).

### G1.17 — Ingest robustness hardening *(review R1–R8 — one batch PR)* — ✅ shipped

- [x] **Per-span error isolation** (`core/proposition.py`, R1): Phase 2 inference and
      Phase 3 persistence each wrap per span, so one failing span (or one failing sample)
      no longer aborts the document. `propositionize_document` returns a
      `PropositionizeReport(action_ids, failed_spans)`; a failed span records **no** extract
      Action, so the next run re-extracts exactly it via the content-addressed idempotency
      check (resume is free — the cache carries it). `StaleExtractionError` /
      `EmbeddingModelMismatchError` stay whole-document fail-loud (Phase 1).
- [x] **Verifier failure = verdict unavailable, not a crash (R2):** a verify call that
      raises (endpoint down past retries, unparseable/uncastable response) leaves
      `faithfulness`/`provisional` null (the degraded G1.1 mode) and records
      `verifier_unavailable` on the verify `Action`; a G1.14 twin's `provisional=True`
      survives. `_verify_all` returns `_VerifyOut | None` per proposition.
- [x] **Kill the zero-vector sentinel** (`core/embeddings.py::pool_span`, R3): returns
      `None` for a no-token span. `persist_spans` skips `None` (and any all-zero vector via
      `_has_no_embedding`, defense-in-depth); `segmentation` substitutes a zero vector for
      its internal adjacency math only and emits one covering span if every sentence is
      token-less; `reembed` leaves an anomalous `None`-pooling row off-target with a warning.
      Invariant upheld: **no zero/None vector reaches pgvector**.
- [x] **Action-lookup indexes (R4):** migration `0010` adds partial functional indexes
      `ix_actions_parse_document_id` / `ix_actions_segment_document_id`
      (`(inputs->>'document_id')`, `timestamp DESC`, partial on `actor`), mirroring `0006`.
      Mirrored in `db/orm.py`; migration notes `actions` is append-only on the hot path,
      partitioning deferred until volume warrants.
- [x] **Overall per-call deadline (R5):** `LLMClient.guided_complete` wraps the whole
      retrying call in `asyncio.timeout(call_timeout_s)` (config `LLM_CALL_TIMEOUT_S`,
      default 180 s — above the tenacity backoff ceiling), so a hung endpoint is cancelled
      and its semaphore permit released rather than starving the batch through full backoff.
- [x] **`EmbeddingSubstrate` lifecycle (R6):** `close()` (idempotent; frees CUDA cache on
      GPU) + `__enter__`/`__exit__`; docstring states a long-running worker holds **one**
      instance, not one per document.
- [x] **`cypher_map` fuzzing (R7):** `hypothesis` property tests of the escaping logic
      (lossless round-trip through the single-quoted Cypher literal; no value can break out)
      + a live-AGE round-trip over an adversarial corpus (quotes, backslashes, agtype/JSON
      fragments, injection attempts, unicode). `db/age.py` now imports DB-free — `settings`
      is lazy-imported inside `cypher()` only — so the pure tests need no `DATABASE_URL`.
      **The fuzz round-trip caught a real second-layer injection:** the SQL wrapper
      `cypher('graph', $$ … $$)` used a fixed `$$` dollar-quote, so a property value containing
      `$$` (LaTeX math, raw `$$` in document text / LLM output) closed the quote early and
      injected raw SQL. Fixed: `cypher()` now picks a **collision-proof** `$iknosN$` tag absent
      from the body (`_dollar_quote_tag`) — `cypher_map` escaping handles the Cypher level, the
      tag handles the SQL level. AGE prepared-statement params remain impossible in the Cypher
      body, so the escaping boundary stays — now fuzzed *and* hardened at both layers.

### G1.18 — Structured table payload in the parse wire contract (§1 rule a) *(review A1)* — ✅ shipped

**Why.** §1 promises "tables ingest as structured observations (rows/cells →
propositions with column semantics)". But `ParseResult` is one reading-order text
blob + linear `[start, end)` ranges; a `ParseKind.TABLE` element is just a char
range, so the 2-D structure (rows, headers, cell adjacency) is destroyed at the
trust boundary and Phase 2's table extractor would have nothing to read.
Retrofitting a wire contract after the MinerU service adapter ships is strictly
more work than adding the slot now.

- [x] Added a `table` payload to `ParseElement` (only valid when `kind == TABLE`):
      `Table{n_rows, n_cols, cells: tuple[TableCell{row, col, start, end, row_span,
      col_span, is_header, bbox?}]}`, mirrored on `OffsetSpec`. **Design note:** cell
      `[start, end)` offsets are **element-relative** (into the element's own text), not
      blob-absolute as the original sketch read — this keeps `ParseElement`
      position-independent (the module's "offsets are derived, never parser-supplied"
      principle; a directly-constructed table element can't know its blob position). They
      are rebased to **document-absolute** in `layouts_for_spans` (the one place the
      element's place in the reading-order text is known), so cell provenance resolves to
      spans and visual provenance still works — the gap goal, reached without violating
      element position-independence.
- [x] **Validation, all fail-loud at construction (so `from_offsets` /
      `MinerUParser` surface it as a hard parse failure):** grid consistency — cells fit
      `n_rows × n_cols`, no two claim the same position after span expansion (sparse and
      merged cells allowed; *not* the strict element-tiling rule) — in
      `Table.__post_init__`; element-relative cell-offset bounds vs the element text and
      "a cell bbox needs the element's frame" in `ParseElement.__post_init__`;
      offset ordering / positive spans in `TableCell.__post_init__`.
- [x] Threaded through the wire schema (`mineru.py::_WireTable`/`_WireCell` →
      `_to_table`), and persisted on the span `layout` dict — `LAYOUT_SCHEMA_VERSION`
      bumped to **2** (a region may now carry a `table`, and may be geometry-less when it
      exists only to carry a table whose element lacked page geometry — so table structure
      is never silently dropped).
- [x] **Consumer stays Phase 2** (cells → observation-class propositions with
      column semantics). This task only makes the structure *survive Stage 0*.
- [x] **Tests:** grid/offset/coupling validators each reject their bad case; a table
      round-trips through `from_offsets` (element-relative, re-validated against the
      slice) and through the MinerU wire client; `layouts_for_spans` rebases cell offsets
      to document-absolute and persists a geometry-less table. (`test_parse.py`,
      `test_mineru.py`.)

### G1.19 — Hybrid-retrieval rank fusion (RRF) + sparse-ranking decision (§4) *(review A3)*

**Why.** The lexical index is Postgres FTS; `ts_rank` is neither TF-IDF nor BM25
(no IDF, no length normalization), so the §4 "BM25" assumption did not hold
(architecture §4 now corrected). Recall of exact tokens is unaffected; score
*fusion* must not trust the scores.

- [ ] When hybrid retrieval is wired (Phase-2/4 consumer), fuse dense + sparse by
      **Reciprocal Rank Fusion** over the two result lists — never a weighted sum
      of cosine and `ts_rank` (incomparable scales).
- [ ] Re-evaluate only if Trial A1 shows the funnel under-recalling on
      lexical-ranked candidates; the upgrade path (ParadeDB `pg_search` /
      VectorChord-BM25) is **AGPL** and requires MinerU-style service-edge
      isolation — flag for the licensing track before adopting.

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
4. ~~**G1.13 slice 1 (truncation guard) + G1.14 (polarity-aware clustering +
   temperature guard)**~~ — ✅ the two critical correctness fixes: `embed_document`
   refuses over-long documents (`DocumentTooLongError`); multi-sample clustering is
   polarity-partitioned with twin quarantine. Landed before Trial A5 threshold fitting.
5. ~~**G1.15 (prompt/schema-hash cache key) + G1.16 (embedding-model identity)**~~ — ✅
   two silent-staleness closures: the extraction cache key now hashes the actual prompt +
   schema (no hand-bumped constant); dense rows carry their `model` with ingest guards
   refusing a swap and `scripts/reembed.py` migrating the index. G1.15 triggers one loud
   full re-extraction on first deploy.
6. ~~**G1.18 (table payload in wire contract)**~~ — ✅ a `TABLE` element carries a validated
   `Table`/`TableCell` grid through the wire schema; element-relative cell offsets rebased to
   document-absolute at persistence (`LAYOUT_SCHEMA_VERSION` → 2). Landed while the wire schema
   is still on a branch, as planned. Consumer stays Phase 2.
7. ~~**G1.13 slice 2 (windowed embedding)**~~ — ✅ long documents embed in overlapping
   macro-windows with full dense coverage (each span pooled from its most-interior window);
   windowing policy folds into `span_content_hash`, window layout recorded on the segment
   Action; slice 1's fail-loud ceiling removed. Single-window path byte-identical.
8. ~~**G1.17 robustness batch**~~ — ✅ one hardening PR: per-span isolation (R1),
   verifier-failure degradation (R2), `pool_span`→`None` (R3), parser/segmenter `actions`
   indexes (R4, migration 0010), per-call deadline (R5), substrate lifecycle (R6),
   `cypher_map` fuzzing (R7).
9. **G1.6 quarantine enforcement** — stakes gating; **still Phase-2-gated** (the `provisional`
   flag is set per node, but no SUPPORTS/REFUTES creation site exists yet to gate at). A Phase 2
   *entry* item — land it when evidential edges are first created.
10. **G1.7b cross-doc reuse** + **G1.8 reference amortization** — remaining cost work.
11. **G1.10 multi-level/summaries**, **G1.11 box**, **G1.12 multi-span**,
    **G1.19 RRF fusion** — incremental, some gated on Phase 2.

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
- [ ] A document **longer than the embedding context** ingests with full dense
      coverage — no silent truncation, no zero vectors in pgvector; window layout
      auditable from the segment Action (G1.13).
- [x] Mixed-polarity extractions can never report full agreement; a
      polarity-unstable span yields `provisional` propositions (G1.14).
- [x] A prompt-template edit alone invalidates the extraction cache (G1.15); an
      embedding-model swap is refused, not silently mixed (G1.16).
