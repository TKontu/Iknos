# Phase 1 — Ingest Pipeline

**Goal:** turn a document into multi-level spans, decontextualized propositions, and
indexes — knowledge *in*, with source references retained throughout.

**Depends on:** Phase 0 (storage, `Document`/`Span`/`Proposition` schema, provenance).
**Architecture refs:** §1 (parse front-end + embedding substrate), §2 (segmentation
backbone), §3 (proposition layer), §4 (indexing), principles 1–3.

**Status:** embedding substrate, segmentation (single-level), proposition layer +
dense/sparse indexes, span persistence (#18), epistemic fields + routing (#20),
extract-then-verify + faithfulness (#21), multi-sample extraction (#23), and version-aware
content-addressed extraction idempotency (G1.7 core, #25) are shipped.
Plain-text ingest runs end-to-end (spans → propositions → indexes → faithfulness from
consistency *and* verification), re-running unchanged content as a no-op and re-extracting a
changed pipeline. The G1.0 parse front-end **contract slice** (swappable parser contract +
identity null parser + `Span.layout` write path + parse provenance) and the **G1.0b MinerU
HTTP client** (`MinerUParser` over our own versioned text+offsets wire schema +
`ParseResult.from_offsets` validated slicer + bytes-in `ingest_document_bytes` entry point +
`make_parser` factory) are shipped. What remains for live MinerU is **standing up the hosted
service** that emits the wire schema (ops/AGPL-side adapter) and **table/figure interpretation**
(Phase 2). Open: MinerU service standup, quarantine enforcement (G1.6), multi-level/RAPTOR
(G1.10), box scoping (G1.11), cross-document cache reuse (G1.7b). The **2026-06 review**
(`archive/review_2026-06_architecture_plan.md`) added **G1.13–G1.19** — two critical correctness
fixes (long-document truncation G1.13, polarity-blind agreement G1.14) plus staleness,
robustness, table-contract, and rank-fusion work. **G1.13 slice 1** (truncation guard) and
**G1.14** (polarity-aware agreement + twin quarantine), **G1.15** (prompt/schema-hash cache
key) and **G1.16** (embedding-model identity column + ingest guards + `reembed` reindex path)
are now shipped — the two critical correctness fixes plus the two silent-staleness closures.
**G1.18** (structured table payload in the parse wire contract) is now shipped too — the
table 2-D structure survives Stage 0. **G1.13 slice 2 (windowed embedding) is now shipped** —
a document longer than the embedding context is embedded in overlapping macro-windows (each
span pooled from the window where it sits furthest from an edge), so long documents ingest
with full dense coverage instead of the slice-1 fail-loud refusal; the windowing policy folds
into the segmentation content hash and the window layout is recorded on the segment Action.
**G1.17 robustness hardening** (R1–R7 — per-span error isolation + a `PropositionizeReport`,
verifier-failure degradation, `pool_span`→`None` killing the zero-vector sentinel, parser/
segmenter `actions` indexes, a per-LLM-call deadline, `EmbeddingSubstrate` lifecycle, and
`cypher_map` property fuzzing) **is now shipped** — one batch hardening the ingest path against
partial failure, hangs, and the hand-rolled escaping boundary. **G1.6 quarantine enforcement**
remains genuinely Phase-2-gated (no SUPPORTS/REFUTES creation site exists yet to gate).
**G1.7b cross-doc reuse is now shipped** — a never-extracted span whose pipeline `content_hash`
matches a prior committed extraction anywhere (re-segmentation, shared boilerplate, an overlapping
reference corpus) replays that extraction's propositions (re-embedded into new nodes, faithfulness
copied, `reused_from` audit pointer) instead of re-running the LLM (`core/reuse.py` + the replay
path in `Propositionizer`; index migration 0012). **G1.8 reference amortization is now shipped** —
a reference-corpus document ingests **once** into a reference/schema-tier box and is sealed
read-only (`(:Document)-[:MEMBER_OF]->(:Box)`); a later investigation re-ingesting identical content
skips the whole pipeline (no embed/segment) instead of repaying it, and a changed-content re-ingest
fails loud (`core/reference_corpus.py` + `ingest_reference_document`). The remaining Phase-1 cost
work is now the optional **G1.12** multi-span provenance.
The **fixture corpus** (exit-criterion seed for the gate corpus / Trial A5) is now shipped —
`tests/fixtures/corpus/` with a long multi-window anchor (G1.13), a polarity-waver anchor
(G1.14), and observation/judgement routing anchors (G1.2), behind a typed model-free loader.
**G1.10 Part A (multi-level offset spans) is now shipped** — the DP length penalty as a
configurable per-level knob (`default_level_policy()`, default 2 levels) producing `Span`s at
multiple granularities from the one cached embedding pass, persisted with per-level idempotency
and purely additive to level 0; **Part B (RAPTOR summaries)** is deferred per the §2 cost
decision. See `archive/gap_phase_1_ingest.md` for the gap-plan IDs.
*(Granular state below; not every box maps 1:1 to a gap ID.)*

## Document parsing — front-end (§1, Stage 0) — 🟡 contract + MinerU client shipped (G1.0/G1.0b)

- [x] **Parser behind a fixed contract** (swappable, like the LLM): `core/parse.py` —
      `ParseElement`/`ParseResult`/`Parser` protocol, reading-order `text` + per-element
      `{page, bbox}`, char ranges derived (no offset drift). `ParseResult.from_offsets`
      (G1.0b) is the real-parser entry: it **slices** element text from the parser's blob at
      supplied offsets (never a second source) and fails loud on bad tiling / dropped text.
- [x] **Invoke MinerU as a separate hosted service** (HTTP client), **not vendored** — it is
      AGPL-3.0; keep the copyleft at the service edge (§1, licensing track). *(G1.0b:
      `core/mineru.py::MinerUParser` POSTs bytes to `PARSER_BASE_URL`, validates a versioned
      response in two gates (pydantic envelope + `from_offsets`), retries transport/5xx only.
      **Standing up the actual MinerU service** that speaks the wire schema is the remaining
      ops step.)*
- [ ] **Tables → structured observations:** ingest table rows/cells as propositions with
      column semantics preserved (observation-class, §3.1); do not flatten to prose.
      *(Phase 2; `ParseKind.TABLE` reserved.)*
- [x] **Table structure survives Stage 0 (G1.18):** `core/parse.py` now carries an
      optional structured `Table`/`TableCell` payload on a `TABLE` `ParseElement` (and
      `OffsetSpec`), threaded through the MinerU wire schema (`_WireTable`/`_WireCell`).
      Cell `[start, end)` offsets are **element-relative** (into the element's own text —
      keeping `ParseElement` position-independent, the module's anti-drift principle) and
      rebased to **document-absolute** at persistence in `layouts_for_spans` (into
      `raw_text`, the coordinate spans live in), so cell provenance resolves to spans and
      visual provenance still works. Grid consistency (cells fit `n_rows × n_cols`, no two
      overlap; sparse/merged cells allowed — *not* the strict element-tiling rule) is
      validated in `Table.__post_init__`; cell-offset-vs-element-text and cell-bbox-needs-
      element-frame in `ParseElement.__post_init__` — both fail loud at the trust boundary.
      `LAYOUT_SCHEMA_VERSION` bumped to 2. Consumer stays Phase 2. *(Review A1.)*
- [ ] **Figures located here, interpreted later:** store figure region + caption + bbox;
      a vision `extract` operator (Phase 2/§3) reads propositions off the figure, flagged
      provisional. *(Phase 2; `ParseKind.FIGURE`/`CAPTION` reserved.)*
- [x] **Carry `{page, bbox}` into `Span`** for visual provenance (claim → region on the
      original page). *(`parse.layouts_for_spans` → `persist_spans(layouts=...)`; versioned
      multi-region layout dict; parse identity folded into the segmentation hash so a
      re-parse correctly invalidates downstream spans.)*
- [~] **Parse quality = faithfulness input:** mark scanned / handwritten / complex-table
      parses lower-faithfulness → provisional → triage; surface MinerU's span/layout
      visualization for expert QA against the original. *(G1.0r — **shipped**: a source span's
      worst `SourceQuality` now folds into faithfulness as a **third independent multiplicative
      factor** beside verify + agreement. `core/parse.py::parse_quality_factor` (policy:
      DIGITAL/None → 1.0, OCR → 0.85, HANDWRITTEN → 0.60 — placeholder Trial-A5 calibration
      constants, the one place the policy lives) + `worst_source_quality` (reads the worst region
      off `Span.layout`, null/version-tolerant); `combine_faithfulness(verify, agreement,
      parse_quality=1.0)` extended (digital/unknown is the identity, so the clean-text path is
      unchanged); threaded per-span through the `Propositionizer` verify fan-out. A badly-parsed
      atom is pulled toward provisional even when the verifier passes it, and cannot be rescued by
      the other signals. **Open:** the parse-quality penalty only applies when a verifier runs
      (faithfulness is null without one — the documented degraded mode); the expert-QA span/layout
      visualization is Phase-7 UI.)*

## Embedding substrate (§1) — built (increment 1); long-document coverage shipped (G1.13)

- [x] Long-context embedding model run **once** per document; contextualized token
      embeddings held for the run ("late chunking" — embed once, derive all
      granularities). *(Scope honesty: the token-embedding cache is per ingest run,
      in memory — not persisted. If G1.10 multi-level re-derivation needs it again,
      persist keyed by `(document, model)` or budget the re-embed; §1.)*
- [x] Confirm boundary detection, multi-level pooling, and search all read from the
      cached vectors (no per-level re-embedding).
- [x] **Truncation guard (G1.13 slice 1 — critical):** *superseded by slice 2.* The
      slice-1 stopgap made an over-long document **fail loudly** (`DocumentTooLongError`)
      instead of silently indexing a prefix. Slice 2 (windowed embedding, below) lifts
      the ceiling entirely, so the refusal — by design a placeholder "until slice 2
      lands" — is removed. The guarantee it protected (no span past the cutoff is
      silently dropped from the dense index) now holds via full windowed coverage.
      *(Review C1.)*
- [x] **Windowed embedding (G1.13 slice 2):** `embed_document` now tokenizes the
      whole document **once without truncation** (content tokens only) and tiles it
      into overlapping macro-windows (`_plan_windows`, overlap `WINDOW_OVERLAP_TOKENS`
      = 1024, a constant not config), one model forward pass per window — each window
      re-framed with the model's own special tokens so interior windows are properly
      bracketed. `DocumentContext` holds the windows; `pool_span(start, end)` selects
      the single window where the span sits **furthest from a window edge** (maximal
      bilateral context) and pools there — never averaged across windows. A document
      that fits one window is the n=1 case, **byte-identical** to the pre-windowing
      path (so segmentation boundary placement is unchanged). The window layout
      (count + boundaries + policy) is recorded on the segment `Action` and the
      windowing **policy** folds into `span_content_hash` (a policy change re-segments;
      one-time loud resegmentation on first deploy, like G1.15). Supersedes slice 1's
      `DocumentTooLongError` ceiling (removed — no length a windowed pass cannot cover).
      Segmentation is transparent to windowing (per-span interior-window selection
      makes adjacent sentences share one context); callers keep their API. *(Review C1.)*

## Segmentation backbone (§2) — built (increment 2; single-level)

- [x] Adjacent-window similarity signal over the cached embeddings; smooth it.
- [x] Valley detection by **depth score** + adaptive threshold (mean − k·σ); not raw
      argmin.
- [x] **DP segmentation** over sentence units: maximize intra-segment coherence minus a
      length penalty (no O(n²) position×size brute force).
- [x] Length penalty as the **level knob** → multiple abstraction levels (sub-paragraph
      … chapter) from one mechanism; store segments as `Span` offset ranges with
      `level`. *(G1.10 **Part A — shipped.** `SegmentationBackbone(levels=…)` takes a
      configurable `list[SegmentLevel]` (the level **count is data**, not code);
      `default_level_policy()` is the default 2-level policy — a fine level 0 + one
      coarse level 1 (4× `max_len`, 1/5 penalty). `segment_document_levels` derives every
      level from the **one** cached embedding pass (embed once — §1/§2); `_ingest_parsed`
      persists each level under its own per-level content hash + segment `Action`, so
      coarse levels are **purely additive** — level 0 stays byte-identical and no existing
      document is force-resegmented on deploy. `_segmented_hash` is now per-`level`. The
      finest level drives the proposition layer; coarse levels ride along under
      `SpanPersistResult.coarse`. Levels are independent granularities — RAPTOR nesting
      with parent links is Part B / Phase-2 `PART_OF`.)*
- [x] Blend an information signal (entity/number density) into the objective so
      segments don't collapse onto redundant blobs.
- [ ] Coarse levels as **summaries**, not just longer windows (RAPTOR-style upward
      tree) — needed so §5.1 coarse-to-fine pruning has crisp parents. *(G1.10 **Part B**
      — deferred; adds ingest-time LLM cost. Part A ships the multi-level offset spans;
      summary generation + parent links is the next increment, gated on the §2 cost
      decision "confirm it's worth the pruning benefit before scaling".)*

## Proposition layer (§3) — built (increment 3)

- [x] Propositionizer: transform sub-paragraph spans into atomic, self-contained
      statements — resolve references, attach qualifiers, split compound claims.
- [x] Store `Proposition` nodes linked to source `Span`(s) (`EVIDENCED_BY`); never free
      text on other nodes.
- [x] Emit an `Action` record per propositionization (model, sampling) (§10.1).

## Extraction faithfulness (§3.1) — harden the perception layer

- [x] **Structured epistemic fields on every proposition** (not flattened into text):
      `polarity` (asserted/negated), `modality` (categorical/probable/possible/
      hypothesized), `attribution` (document/reported-speech/named-source), `scope`, and
      **`epistemic_class`** (observation / testimony / judgement — orthogonal to modality).
      *(G1.1, #20.)*
- [x] **Extract observations as facts; do not inherit source conclusions (§3.1, §5).**
      Classify each proposition's `epistemic_class`; ingest a source's *observations* as
      facts and its *conclusions* as defeasible, credibility-weighted judgement-claims —
      never as facts. The engine re-derives conclusions from the observations.
      *(G1.2 routing, #20; the consuming extraction is Phase 2.)*
- [x] **Multi-sample extraction:** sample the extractor N times, cluster equivalent
      extractions, score each by cross-sample agreement. *(G1.3, #23; `core/consistency.py`
      + `combine_faithfulness` — agreement folds into `faithfulness` multiplicatively.
      Default `LLM_EXTRACT_SAMPLES=1` = no-op; per-model calibration is Trial A3.)*
- [x] **Polarity-aware agreement (G1.14 — critical):** clustering now runs only
      *within* identical `(polarity, epistemic_class)` partitions
      (`consistency.cluster_candidates_partitioned`) — embedding cosine cannot
      distinguish a claim from its negation, so a 3-assert/2-negate split now yields a
      0.6 and a 0.4 cluster, never one 1.0 cluster. `consolidate_samples` then detects
      cross-polarity **twins** (opposite polarity, medoid cosine ≥ threshold), sets
      both halves `provisional` (OR-folded so the verify pass cannot clear it), and
      records the twin pairing on the extract `Action.outputs` for Trial A5. The
      `LLM_EXTRACT_SAMPLES > 1` ⇒ temperature > 0 config guard was already enforced at
      `Propositionizer` construction. Landed **before** Trial A5 fits the threshold.
      *(Review C2/P4.)*
- [x] **`verify` step:** entailment/NLI check that the span supports the proposition
      *with its polarity and modality*; disagreement sets `provisional` (§3.1). Prefer an
      **independent verifier (different model family from the extractor)** to reduce
      correlated error (§13). *(G1.4, #21 — `core/verify.py`; optional, configured via
      `LLM_VERIFIER_*`.)*
- [x] Record a `faithfulness` ∈ [0,1] per proposition — kept **distinct** from source
      credibility (§9) and evidential strength (§8). *(G1.5, #21 — derived from the
      verify verdict, never self-reported.)*
- [ ] **Quarantine by stakes:** provisional/low-faithfulness propositions may exist but
      cannot drive high-stakes moves (e.g., a `REFUTES`) until confirmed; route to the
      expert-triage queue. *(G1.6 — `provisional` is now set per node; edge-time
      enforcement is gated on Phase 2 evidential edges.)*
- [ ] Faithfulness gate metric wired for the trial plan (entailment, negation/modality
      preservation) — see `todo_trials.md` A5. *(Decomposed verdicts persisted in
      `actions.outputs`; computing the metric on a labeled corpus remains.)*

## Indexing (§4)

- [x] **Dense** index in pgvector over the chosen granularities. *(increment 3 —
      `proposition_embeddings`.)*
- [x] **Sparse/lexical** index (exact-token) — catches names, codes, acronyms.
      *(increment 3 — `proposition_lexical_index`, `simple` tsvector + GIN. Honesty
      note: Postgres `ts_rank` is **not** TF-IDF/BM25 — recall is fine, ranking
      semantics differ; §4 corrected. Review A3.)*
- [ ] **Rank-based fusion (G1.19):** hybrid dense+sparse retrieval fuses by
      Reciprocal Rank Fusion, never a weighted sum of cosine and `ts_rank`
      (incomparable scales). AGPL BM25 extensions only if Trial A1 shows
      under-ranking, and only service-isolated like MinerU.
- [ ] Both indexes carry `box` id so retrieval can be scoped to the active working set.
      *(G1.11 — gated on Phase 2 boxing.)*
- [x] **Embedding-model identity (G1.16):** `model TEXT NOT NULL` column on
      `document_embeddings`/`proposition_embeddings` (migration `0008`) + mismatch guard
      (`EmbeddingModelMismatchError`, raised in `ingest.persist_spans` for spans and
      `proposition._guard_embedding_model` for propositions) + `scripts/reembed.py`
      (over `core/reembed.py`) migration path — a same-dimension model swap is now
      refused and migrated, not silently mixed into one ANN space. *(Review A5.)*
- [ ] (Keyword/entity index feeds graph nodes in Phase 2 and candidate generation in
      Phase 4 — keep keyworders in the lexical layer, not the graph.)

## Cost & incrementality (§6.1)

- [x] **Content-addressed cache** for LLM outputs (propositions, extractions) keyed by
      content + model version; unchanged spans are never re-inferred ("extract once").
      *(G1.7 core, #25: extraction idempotency is version-aware — keyed on `(span_id,
      content_hash)` over the extractor model/prompt/regime/verifier (`core/cache.py`).
      Unchanged content no-ops; a changed pipeline re-extracts (or fails loud). **G1.7b — shipped:**
      cross-document output reuse — a never-extracted span whose `content_hash` matches a prior
      committed extraction anywhere replays its propositions (re-embedded, new nodes, copied
      faithfulness, `reused_from` audit pointer) instead of re-running the LLM (`core/reuse.py`,
      migration 0012); on by default, gated behind the `stored is None` branch so a no-op re-run
      pays nothing extra. **G1.7r — shipped:** cascade re-extraction on a stale span — a span
      re-run under a changed pipeline now purges its superseded propositions (+ `EVIDENCED_BY`
      edges + dense/lexical index rows) and re-extracts in one transaction, instead of the
      fail-loud `StaleExtractionError`; refuses (`CascadeDependentsError`) if the propositions
      already feed downstream nodes (the deferred full cascade), and `cascade_reextract=False`
      restores the conservative fail-loud mode.)*
- [x] **Hash the real prompt + schema into the cache key (G1.15):** `prompt_sha`/`schema_sha`
      (extractor *and* verifier) now feed `extraction_content_hash`, so a prompt edit
      re-extracts even without a hand-bumped `EXTRACT_SCHEMA_VERSION`; the version stays a
      *semantic* output-shape marker. `schema_sha` is key-order-insensitive
      (`cache.canonical_json_sha256`). One-time loud full re-extraction on first deploy.
      *(Review A4.)*
- [x] **Amortize reference processing (G1.8) — shipped:** reference-corpus / domain-pack
      boxes are ingested **once** and persisted read-only for reuse across investigations;
      only case documents are processed per investigation (§9). *(`core/reference_corpus.py`
      + `core/ingest.py::ingest_reference_document`.)* A reference document ingests into a
      **reference/schema-tier** box (`reference_box` / registry create-or-noop) and is sealed
      by a `(:Document)-[:MEMBER_OF]->(:Box)` edge carrying `{tier, sealed, input_sha256,
      valid_from}` + a `seal-reference` Action; the seal keys on the document's own content
      digest, not the parse/segment hash. **Amortization is real:** a re-ingest of identical
      content short-circuits *before* `substrate.embed_document` (no embed, no segment, no
      writes) and returns `reused=True` — content-addressed caching (G1.7) already no-op'd the
      *writes* but still paid the embedding pass; the seal lets a later investigation pay zero
      to reuse the corpus (§6.1 "amortized, not repaid"). **Read-only by construction:** a
      changed-content re-ingest (or re-seal into a different box) raises `ReferenceSealError`
      (mirrors `PackImmutabilityError` — bump the version / new id), and a `case`/`working`
      box is refused up front (`validate_sealable_tier` → `ValueError`). Depends on G0.7
      (shipped) + box tier (G2.1, shipped); no migration (new AGE edge label + node property
      over the existing `actions` table). Tests: `tests/unit/test_reference_corpus.py` +
      live-AGE `tests/integration/test_reference_corpus.py`. Seams (not this slice): a
      bytes-in `ingest_reference_document_bytes` (trivial — keys on the same digest) and
      box-scoped indexing of reference spans (G1.11, still gated on the ingest-box decision).

## Robustness hardening (G1.17, review R1–R8 — one batch PR) — ✅ shipped

- [x] **Per-span error isolation (R1)** in the propositionizer: Phase 2 inference and
      Phase 3 persistence each isolate per span — one flaky span/sample no longer aborts
      the document. `propositionize_document` returns a `PropositionizeReport`
      (`action_ids` + `failed_spans{span_id, phase, error}`); a failed span records **no**
      Action, so the next run re-extracts exactly it via the content-addressed idempotency
      check (resume is free). Whole-document contract violations (`StaleExtractionError`,
      `EmbeddingModelMismatchError`) stay fail-loud.
- [x] **Verifier failure degrades, not crashes (R2):** a verify call that raises (endpoint
      down past retries, unparseable/uncastable response) leaves `faithfulness`/`provisional`
      null (the documented G1.1 degraded mode) and records `verifier_unavailable` on the
      verify `Action` — never an exception mid-batch. A G1.14 twin's `provisional=True`
      survives the degraded path.
- [x] **`pool_span` returns `None` for no-token spans (R3)** — the zero-vector sentinel is
      gone. `persist_spans` skips `None` (and, defense-in-depth, any all-zero vector via
      `_has_no_embedding`); `segmentation` substitutes a zero vector for its *internal*
      adjacency math only (never persisted) and emits one covering span if every sentence is
      token-less; `reembed` leaves an anomalous `None`-pooling row off-target with a warning.
      Invariant: no zero/None vector reaches pgvector.
- [x] **Partial functional `actions` indexes (R4):** migration `0010` adds
      `ix_actions_parse_document_id` / `ix_actions_segment_document_id`
      (`(inputs->>'document_id')`, `timestamp DESC`, partial on actor) mirroring `0006`'s
      propositionizer index — the parse/segment idempotency lookups are O(log n) again.
      Mirrored in `db/orm.py`. Note in the migration: `actions` is append-only on the hot
      path; table partitioning deferred until volume warrants.
- [x] **Per-LLM-call deadline (R5):** `guided_complete` wraps the whole retrying call in an
      `asyncio.timeout(call_timeout_s)` (config `LLM_CALL_TIMEOUT_S`, default 180 s, above
      the tenacity backoff ceiling) — a hung endpoint is cancelled and its semaphore permit
      released instead of starving the batch through full backoff.
- [x] **`EmbeddingSubstrate` lifecycle (R6):** `close()` (idempotent; frees CUDA cache on
      GPU) + context-manager support; docstring states a long-running worker holds **one**
      instance, not one per document.
- [x] **`cypher_map` fuzzing (R7):** property-based (`hypothesis`) tests of the escaping
      logic — a string round-trips losslessly through the single-quoted Cypher literal and no
      value can break out of it — plus a live-AGE round-trip over an adversarial corpus
      (quotes, backslashes, agtype/JSON fragments, injection attempts, unicode). `cypher_map`
      is now import-DB-free (`settings` lazy-imported in `cypher()` only) so the pure tests
      need no `DATABASE_URL`. **The fuzz round-trip found a real injection:** a value
      containing `$$` broke out of the SQL `cypher('graph', $$ … $$)` dollar-quote; `cypher()`
      now uses a collision-proof `$iknosN$` tag (`_dollar_quote_tag`), closing the SQL-level
      half of the boundary that `cypher_map` does not cover.

## Exit criteria

- [ ] A document ingests end-to-end: cached embeddings → multi-level spans →
      propositions → dense + sparse indexes, all with retained span references.
- [ ] Hybrid retrieval (dense + sparse), box-scoped, returns propositions with their
      source text resolvable.
- [x] Re-ingesting an unchanged document hits the cache (no re-extraction); a static
      reference corpus is processed once and reused. *(Cache no-op + cross-document "extract once"
      reuse (G1.7/G1.7b) **and** the read-only reference-corpus amortization across
      investigations (G1.8 — `ingest_reference_document` skips the whole pipeline on a sealed
      re-ingest) are all shipped.)*
- [x] A document longer than the embedding context ingests with **full** dense
      coverage — no silent truncation (G1.13 slice 2: windowed embedding). No zero
      vector reaches pgvector: `pool_span` now returns `None` for a no-token span and
      `persist_spans` skips it (G1.17 R3); the legacy zero-vector sentinel is gone, with
      an all-zero check kept as defense-in-depth.
- [x] Mixed-polarity extractions never report full agreement; polarity-unstable
      spans yield `provisional` propositions (G1.14).
- [x] A prompt-template edit alone invalidates the extraction cache (G1.15); an
      embedding-model swap is refused, not silently mixed (G1.16).
- [x] **Fixture corpus (seed for the gate corpus) — shipped.** `tests/fixtures/corpus/`
      holds three real documents + a `manifest.toml` of machine-readable regression
      anchors, loaded by a typed, model-free/DB-free loader (`tests/fixtures/corpus.py`,
      stdlib `tomllib`) and kept honest by `tests/unit/test_corpus.py`. It includes a
      document **longer than one embedding window** — `long_case_file.txt`, > 8200 words,
      so `tokens ≥ words > MAX_MODEL_TOKENS` makes ">1 window" provable in CI **with no
      model in the loop** (G1.13 tail-coverage anchor; the judgement anchor sits in the
      tail) — and a span **whose negation the extractor wavers on** (`polarity_waver.txt`,
      the `"ambiguous"` polarity sentinel: must yield split clusters + a `provisional`
      proposition, G1.14). Anchors carry **quotes, not hand-counted offsets** (the loader
      locates each and asserts it is unique). The model-backed end-to-end run + gate
      metric over this corpus is Trial A5; this is the labelled input it consumes.

## Phase risks / decisions

- The DP objective blend (coherence vs information signal) is a knob to tune, not a
  research goal (§8) — don't over-engineer it.
- Summary-based coarse levels add LLM cost at ingest; confirm it's worth it for the
  pruning benefit before scaling.

## Build record *(merged from `archive/gap_phase_1_ingest.md`, 2026-06-11; full per-item rationale in `docs/archive/`)*

Shipped, one line per item (PRs #18–#48): **G1.0/G1.0b** parse contract + null parser +
MinerU HTTP client over our own versioned wire schema (AGPL stops at the service edge);
**G1.1/G1.2** structured epistemic fields + observation/judgement routing; **G1.3**
multi-sample extraction (`core/consistency.py`, agreement folds multiplicatively into
faithfulness); **G1.4/G1.5** extract-then-verify (independent verifier seam) + derived
faithfulness; **G1.7** version-aware content-addressed cache
(`(span_id, content_hash)`, `StaleExtractionError`); **G1.7b** cross-doc "extract once"
replay (`core/reuse.py`); **G1.8** reference-corpus amortization (sealed read-only
boxes, `ReferenceSealError`, re-ingest skips embed/segment/persist entirely); **G1.9**
span persistence (deterministic `uuid5` ids, immutability guard); **G1.10 Part A**
multi-level offset spans (one embedding pass, per-level content hash, coarse levels
purely additive); **G1.13** windowed late chunking (overlapping macro-windows,
most-interior-window pooling, policy folds into `span_content_hash`); **G1.14**
polarity-partitioned agreement clustering + polarity-twin quarantine + degenerate-
sampling guard; **G1.15** prompt/schema-SHA cache keys (no hand-bumped constant);
**G1.16** embedding-model identity column + mismatch guards + `scripts/reembed.py`;
**G1.17** robustness batch (per-span isolation, verifier degradation, `pool_span`→
`None`, action indexes, per-call deadline, substrate lifecycle, `cypher_map` fuzzing —
which caught and fixed the `$$` dollar-quote SQL injection, now `_dollar_quote_tag`);
**G1.18** structured table payload in the wire contract (element-relative cell offsets,
rebased at persistence, `LAYOUT_SCHEMA_VERSION` 2); fixture corpus seed
(`tests/fixtures/corpus/`, quote-anchored manifest, model-free loader).

## Open work — carried from the gap plan *(specs preserved; execute as written)*

- [ ] **G1.0 remainder — stand up the live MinerU service** *(ops, not code)*: the
      service-side adapter that speaks our wire schema (`core/mineru.py` client is
      done). Tracked in the deployment runbook entry criterion
      (`todo_phase_6_investigation_runtime.md`).
- [ ] **G1.0 remainder — tables → observation propositions** *(Phase 2 consumer)*:
      cells → propositions with column semantics, observation-class (§3.1); the 2-D
      structure already survives Stage 0 (G1.18). Figures: located now
      (`ParseKind.FIGURE`/`CAPTION` reserved), interpreted by a Phase-2 vision
      `extract` operator, provisional.
- [x] **G1.0 remainder — parse quality → faithfulness input** *(G1.0r — shipped)*:
      `SourceQuality` (per element/region) is now consumed in the faithfulness derivation —
      `parse_quality_factor` × the verify and agreement signals in `combine_faithfulness`,
      threaded per source span through the propositionizer (`core/parse.py`,
      `types/epistemic.py`, `core/proposition.py`). Lower-quality parse → lower faithfulness →
      provisional → triage. (Penalty constants are a Trial-A5 calibration seam; applies on the
      verifier path; expert-QA layout visualization is Phase-7 UI.)
- [ ] **G1.5 remainder — Trial A5 faithfulness-gate metric**: the decomposed verify
      verdicts are persisted in `actions.outputs` ready for the metric; computing it
      on the labeled gate corpus is harness work (V3 in `todo_trials.md`).
- [x] **G1.7 remainder — cascade re-extraction** *(G1.7r — shipped)*: a stale span (changed
      pipeline) now purges its superseded propositions + `EVIDENCED_BY` edges + dense/lexical
      index rows and re-extracts **in one transaction** (`Propositionizer._purge_span_propositions`
      / `_persist(purge_existing=…)`), recording a `superseded` audit pointer — instead of the
      fail-loud `StaleExtractionError`. **On by default** (`cascade_reextract=True`); `False`
      restores fail-loud. **Bounded to Phase-1 output:** a span whose propositions already feed
      downstream (Phase-2+) nodes raises `CascadeDependentsError` rather than orphaning them — the
      full downstream cascade stays the deferred resegmentation-cascade work (`ingest.py`).
      Verified on live AGE (`tests/integration/test_extraction_cache.py`).
- [ ] **G1.10 Part B — RAPTOR summary levels** *(deferred; trigger in `todo.md`)*:
      coarse levels as summaries (RAPTOR-style upward tree), not just longer windows —
      needed so §5.1 coarse-to-fine pruning has crisp parents. Adds ingest-time LLM
      cost; gated on the §2 "confirm it's worth the pruning benefit" decision. Note:
      no production ingest entrypoint constructs the multi-level segmenter yet (tests
      inject it); wire it to `default_level_policy()` when that entrypoint lands.
- [ ] **G1.11 — `box` on the dense/sparse indexes** *(trigger: first hybrid-retrieval
      consumer)*: add `box` to `proposition_embeddings`/`proposition_lexical_index` so
      retrieval scopes to the active working set (§4). Blocked on an architectural
      decision: propositions are indexed in Phase 1 *before* any box is assigned at
      Phase-2 extract time — decide how a box threads through ingest first.
- [ ] **G1.12 — multi-span provenance** *(optional)*: add `EVIDENCED_BY` to the
      context spans a proposition drew on (today: target span only; context ids live
      in `Action.inputs`).
- [ ] **G1.19 — hybrid-retrieval rank fusion** *(trigger: same consumer as G1.11)*:
      fuse dense + sparse by **Reciprocal Rank Fusion** over the two result lists —
      never a weighted sum (cosine and `ts_rank` are incomparable; `ts_rank` is
      neither TF-IDF nor BM25). Re-evaluate only if Trial A1 shows under-recall on
      lexical candidates; the upgrade path (ParadeDB `pg_search` / VectorChord-BM25)
      is **AGPL** → MinerU-style service-edge isolation, flag for the licensing track.
- [ ] **G1.6 — quarantine enforcement**: moved to the Phase 4 safety lockdown
      (R8 → R9 → V7) now that the `REFUTES` creation site exists — see
      `todo_phase_4_linking_adjudication.md`.

### From the ingest decision thread *(D1/D2 deltas, merged from `archive/todo_ingest.md` 2026-06-11 — the decisions now live in §3.1/§6.1/§2; these are the code changes they require)*

- [ ] **G1.20 — `calibrate(agreement)` in the combiner (D1 delta).**
      `combine_faithfulness` is shipped multiplicative with the calibration seam
      documented but identity. Add `calibrate(agreement)` per §3.1: a mild concave /
      Wilson-style map over the raw agreement (small-N is coarse — N=3 → {0, ⅓, ⅔, 1}),
      identity until Trial A3 fits the per-model curve; the **raw** agreement stays the
      persisted value (calibration at combine time only, so the curve can change
      without rewriting stored data). Land the function + config seam before A5 fits
      `PROP_AGREEMENT_THRESHOLD`, even while the curve is identity — the threshold must
      be calibrated against the final code path. Tests: identity curve ⇒ behavior
      byte-identical to today; a non-identity fixture curve moves faithfulness in the
      conservative direction only.
- [x] **G1.21 — degraded-mode `null` ⇒ provisional (D2 behavior change)** *(shipped)*.
      Pre-G1.21 behavior left `provisional` **null** when the verifier is off; §3.1 now decides
      `faithfulness = null` (unassessed) ⇒ **provisional = true** with reason
      *unassessed faithfulness* — never coerce null toward trusted. `types/epistemic.py`:
      `ProvisionalReason` gains `UNASSESSED_FAITHFULNESS` and `provisional_reasons_for(None)`
      returns `{UNASSESSED_FAITHFULNESS}` (the R8 spec amended accordingly). `core/proposition.py`:
      a single `_with_faithfulness_reason` helper OR-folds the faithfulness-axis reason onto a
      finalized result (idempotent; never clears an extract-time reason — a G1.14 twin keeps
      `POLARITY_UNSTABLE` and carries `UNASSESSED_FAITHFULNESS` too), applied at all three
      result-finalization sites: the verify-success path, the per-span verifier-unavailable
      degraded path (G1.17 R2), the verifier-off-entirely path, and the G1.7b replay builder — so
      node == in-memory result == extract/verify `Action` rows everywhere. `core/reuse.py`:
      legacy pre-R8 reconstruction stays frozen (a `provisional=true` + null-faithfulness node was
      always a polarity twin, so it reconstructs `POLARITY_UNSTABLE`, *not* the new
      `UNASSESSED_FAITHFULNESS`; the replay write path re-folds the live reason regardless).
      Degraded-mode tests repinned **deliberately** (G1.3/G1.17): `test_provisional_reasons_*`,
      `test_verify_all_degrades_on_verifier_failure`,
      `test_verify_all_failure_preserves_twin_provisional`,
      `test_multi_sample_without_verifier_sets_agreement_only`,
      `test_verifier_absent_leaves_faithfulness_null`. (V7 edge enforcement now holds these atoms
      back from high-stakes moves; G1.22 backfill later completes their faithfulness.)
- [ ] **G1.22 — verification as its own cached stage (the fix D2 needs to work).**
      §3.1 promises "when the verifier is later enabled, faithfulness completes from
      persisted agreement *without re-sampling*" — but the verifier signature is
      currently folded into the **extraction** content hash (G1.15), so toggling or
      upgrading the verifier trips `StaleExtractionError` → a full re-extraction, not a
      cheap completion. Fix: **remove the verifier signature from the extraction key**
      (the extractor's output does not depend on the verifier) and key verification as
      its own idempotent stage — per proposition, `(proposition_id, verify_sig)` over
      the existing verify `Action`s — with a **verify-backfill** entrypoint that runs
      the verifier over already-extracted propositions, computes
      `combine_faithfulness(verify, stored agreement)`, updates
      `faithfulness`/`provisional`(+reasons) in place, and records a verify `Action`
      per proposition. Consequences to handle: one-time loud re-key on deploy (the
      extraction hash changes — same class as G1.15's first deploy, correct and loud);
      G1.7b replay copies faithfulness only when the *verify* stage identity matches,
      else replays the extraction and queues the spans for verify-backfill.
      **Interaction with shipped G1.7r (#68):** cascade re-extraction currently
      fires on *any* pipeline-identity change including the verifier sig — after
      this split, a verifier-only change must trigger **verify-backfill, not
      cascade re-extraction** (no propositions are purged for a verifier change);
      update G1.7r's trigger condition accordingly. Tests:
      verifier off→on completes faithfulness with **zero** extractor LLM calls and
      **zero** purged propositions; verifier upgrade re-verifies without
      re-extracting; extraction cache hit-rate unaffected by verifier config.

### From the 2026-06-11 architecture assessment *(W-tasks; findings record in `archive/review_2026-06-11_planned_architecture_assessment.md`)*

- [ ] **G1.23 (W5) — enforce nonzero sampling temperature when `n_samples > 1`.**
      §3.1 is explicit: "Multi-sample also requires nonzero sampling temperature,
      or N identical samples make agreement trivially perfect; **the configuration
      must enforce this, not document it**." The shipped defaults are
      `temperature: 0.0` (`core/proposition.py`, `core/extract.py`,
      `core/verify.py`) with no guard — a multi-sample run today returns
      agreement = 1.0 vacuously, silently inflating faithfulness. Add constructor
      validation on the multi-sample extraction/verify paths: `n_samples > 1`
      with `temperature == 0` raises at construction. **Exemption (by design):**
      the G4.3 edge judge derives its sample diversity from the per-sample
      permutation at temperature 0 — document the exemption where the guard
      lives, so nobody "fixes" the judge. Tests: construction raises; `n=1, T=0`
      passes; the guard trips with zero LLM calls.
- [ ] **G1.24 (W6) — context span identity in the extraction cache key.** The key
      folds the rendered `context_text` but not *which spans* produced it
      (`core/cache.py`; context assembly in `core/proposition.py`): a
      re-segmentation that changes the K-span context window can serve a stale
      extraction — or thrash — on textually-similar context. Add the ordered
      context `span_id`s to the extraction content hash so cache identity is
      deterministic on ingest identity. One-time loud re-key on deploy (same
      class as G1.15/G1.22 — correct and loud); **coordinate with G1.22 so the
      key changes once, not twice**. Tests: same target span + changed context
      span set ⇒ different key ⇒ re-extraction; unchanged ingest ⇒ byte-identical
      key.
