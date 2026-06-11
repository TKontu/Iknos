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
(`review_2026-06_architecture_plan.md`) added **G1.13–G1.19** — two critical correctness
fixes (long-document truncation G1.13, polarity-blind agreement G1.14) plus staleness,
robustness, table-contract, and rank-fusion work. **G1.13 slice 1** (truncation guard) and
**G1.14** (polarity-aware agreement + twin quarantine), **G1.15** (prompt/schema-hash cache
key) and **G1.16** (embedding-model identity column + ingest guards + `reembed` reindex path)
are now shipped — the two critical correctness fixes plus the two silent-staleness closures.
**G1.18** (structured table payload in the parse wire contract) is now shipped too — the
table 2-D structure survives Stage 0. G1.13 slice 2 (windowed embedding) is the new front of
the queue. See
`gap_phase_1_ingest.md` for the gap-plan IDs. *(Granular state below; not every box maps
1:1 to a gap ID.)*

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
- [ ] **Parse quality = faithfulness input:** mark scanned / handwritten / complex-table
      parses lower-faithfulness → provisional → triage; surface MinerU's span/layout
      visualization for expert QA against the original. *(`SourceQuality` carried now;
      consumed in G1.5/G1.6.)*

## Embedding substrate (§1) — built (increment 1); long-document coverage open (G1.13)

- [x] Long-context embedding model run **once** per document; contextualized token
      embeddings held for the run ("late chunking" — embed once, derive all
      granularities). *(Scope honesty: the token-embedding cache is per ingest run,
      in memory — not persisted. If G1.10 multi-level re-derivation needs it again,
      persist keyed by `(document, model)` or budget the re-embed; §1.)*
- [x] Confirm boundary detection, multi-level pooling, and search all read from the
      cached vectors (no per-level re-embedding).
- [x] **Truncation guard (G1.13 slice 1 — critical):** a document longer than the
      model context (8192 tokens) now **fails loudly** (`DocumentTooLongError`)
      instead of silently indexing a prefix. *(`core/embeddings.py`:
      `embed_document` tokenizes **without** truncation and guards on the true token
      count via the pure `_raise_if_truncated` (`MAX_MODEL_TOKENS`) before any forward
      pass — so no partial index is ever written for an over-long document. Review C1.
      Windowed embedding (slice 2) lifts the ceiling.)*
- [ ] **Windowed embedding (G1.13 slice 2):** overlapping macro-windows over long
      documents; each span pooled from the window where it sits furthest from a
      window edge; window layout recorded in the segment Action and folded into
      the span content hash. Needed before MinerU feeds real multi-page PDFs.

## Segmentation backbone (§2) — built (increment 2; single-level)

- [x] Adjacent-window similarity signal over the cached embeddings; smooth it.
- [x] Valley detection by **depth score** + adaptive threshold (mean − k·σ); not raw
      argmin.
- [x] **DP segmentation** over sentence units: maximize intra-segment coherence minus a
      length penalty (no O(n²) position×size brute force).
- [ ] Length penalty as the **level knob** → multiple abstraction levels (sub-paragraph
      … chapter) from one mechanism; store segments as `Span` offset ranges with
      `level`. *(G1.10 — the `level` field exists, default 0; multi-level generation is
      not yet wired.)*
- [x] Blend an information signal (entity/number density) into the objective so
      segments don't collapse onto redundant blobs.
- [ ] Coarse levels as **summaries**, not just longer windows (RAPTOR-style upward
      tree) — needed so §5.1 coarse-to-fine pruning has crisp parents. *(G1.10.)*

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

- [~] **Content-addressed cache** for LLM outputs (propositions, extractions) keyed by
      content + model version; unchanged spans are never re-inferred ("extract once").
      *(G1.7 core, #25: extraction idempotency is version-aware — keyed on `(span_id,
      content_hash)` over the extractor model/prompt/regime/verifier (`core/cache.py`).
      Unchanged content no-ops; a changed pipeline re-extracts (or fails loud). Cross-document
      output reuse — "extract once" across docs/re-segmentation — is the remaining G1.7b.)*
- [x] **Hash the real prompt + schema into the cache key (G1.15):** `prompt_sha`/`schema_sha`
      (extractor *and* verifier) now feed `extraction_content_hash`, so a prompt edit
      re-extracts even without a hand-bumped `EXTRACT_SCHEMA_VERSION`; the version stays a
      *semantic* output-shape marker. `schema_sha` is key-order-insensitive
      (`cache.canonical_json_sha256`). One-time loud full re-extraction on first deploy.
      *(Review A4.)*
- [ ] **Amortize reference processing:** reference-corpus / domain-pack boxes are ingested
      **once** and persisted read-only for reuse across investigations; only case
      documents are processed per investigation (§9).

## Robustness hardening (G1.17, review R1–R8 — one batch PR)

- [ ] Per-span error isolation in the propositionizer (`gather` must not let one
      flaky span/sample abort the document; failed spans recorded + resumable via
      idempotency).
- [ ] Verifier failure degrades to "verdict unavailable" (faithfulness/provisional
      null, logged) instead of crashing the batch.
- [ ] `pool_span` returns `None` for no-token spans — no zero-vector sentinel; no
      zero vector can ever reach pgvector.
- [ ] Partial functional `actions` indexes for parser/segmenter idempotency lookups
      (migration 0006 covered only the propositionizer).
- [ ] Per-LLM-call `asyncio.timeout` above the tenacity ceiling (a hung endpoint
      must not hold a semaphore permit through full backoff).
- [ ] `EmbeddingSubstrate` close()/context-manager lifecycle.
- [ ] Property-based fuzz tests for `cypher_map` escaping (document text and LLM
      output cross this hand-rolled boundary).

## Exit criteria

- [ ] A document ingests end-to-end: cached embeddings → multi-level spans →
      propositions → dense + sparse indexes, all with retained span references.
- [ ] Hybrid retrieval (dense + sparse), box-scoped, returns propositions with their
      source text resolvable.
- [ ] Re-ingesting an unchanged document hits the cache (no re-extraction); a static
      reference corpus is processed once and reused.
- [ ] A document longer than the embedding context ingests with **full** dense
      coverage — no silent truncation, no zero vectors in pgvector (G1.13).
- [x] Mixed-polarity extractions never report full agreement; polarity-unstable
      spans yield `provisional` propositions (G1.14).
- [x] A prompt-template edit alone invalidates the extraction cache (G1.15); an
      embedding-model swap is refused, not silently mixed (G1.16).
- [ ] Maintain a small fixture corpus exercising this path (seed for the gate corpus).
      Include at least one document longer than one embedding window and one span
      whose negation the extractor is known to waver on (regression anchors for
      G1.13/G1.14).

## Phase risks / decisions

- The DP objective blend (coherence vs information signal) is a knob to tune, not a
  research goal (§8) — don't over-engineer it.
- Summary-based coarse levels add LLM cost at ingest; confirm it's worth it for the
  pruning benefit before scaling.
