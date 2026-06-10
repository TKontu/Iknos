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
(G1.19). G1.13 slice 1 + G1.14 jump the queue: they are cheap and stop silent data
corruption; do them **before** any further perception-layer tuning and before Trial
A5 fits thresholds.

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

- [ ] **Slice 1 — fail-loud guard (do first; tiny).** In `embed_document`, detect
      truncation (tokenize without truncation and compare length, or check
      `seq_len == max_length`) and raise a new `DocumentTooLongError` (same
      pattern/placement as `DocumentResegmentationError`). No partial index may be
      written. Unit test: text tokenizing past the limit raises; text under it does
      not. This converts silent data loss into a loud refusal until Slice 2 lands.
- [ ] **Slice 2 — overlapping macro-windows ("late chunking over windows").**
      Embed consecutive max-length windows with a fixed token overlap (start at
      1024; make it a constant, not config). `DocumentContext` becomes a list of
      windows, each with its own `token_embeddings` + char-offset mapping;
      `pool_span(start_char, end_char)` selects the window where the span sits
      **furthest from a window edge** and pools there (never average across
      windows). Callers (`ingest.py`, `segmentation.py`) keep their current API.
- [ ] **Provenance + idempotency:** record the window layout (window count,
      boundaries, overlap, model max length) in the segment `Action.inputs`, and
      fold it into the `span_content_hash` inputs so a changed windowing policy
      re-segments instead of silently reusing spans.
- [ ] **Segmentation across windows:** compute adjacent-window similarities within
      each macro-window; in overlap zones take the values from the window where both
      compared positions are interior. Boundary placement must be identical for a
      document that happens to fit one window.
- [ ] **Tests:** a synthetic document spanning >2 windows gets a non-zero dense
      vector for *every* span; a span straddling a window seam pools from the
      interior window; ingest of the same long document twice is a no-op; the
      single-window path is byte-identical to today's behaviour.

### G1.14 — Polarity-aware agreement clustering + degenerate-sampling guard (§3.1) *(review C2 — **critical**, inflated confidence on negation flips)*

**Why.** `core/consistency.py::cluster_candidates` forms clusters by embedding
cosine alone (`threshold 0.86`). Sentence embeddings place a claim and its negation
nearly on top of each other (typically cosine > 0.9), so asserted and negated
variants of the same claim co-cluster: 3-assert/2-negate across 5 samples yields
**agreement 1.0** — maximum confidence on precisely the polarity instability §3.1
exists to catch — and `canonical_of` makes the persisted polarity a sample-
distribution coin flip. The `Candidate` dataclass already carries the fields; they
are just unused for identity.

- [ ] **Hard-partition before clustering:** run cosine clustering only *within*
      groups of identical `(polarity, epistemic_class)`. Modality stays soft (it
      varies legitimately across phrasings); polarity and epistemic class are
      identity. Implement as a `groupby` wrapper around the existing
      `cluster_candidates` — keep the inner algorithm untouched.
- [ ] **Cross-polarity instability is a negative signal, not noise:** after
      partitioned clustering, detect **polarity twins** — clusters in opposite
      polarity partitions whose representatives' cosine ≥ threshold. For twins:
      each side's `agreement` stays its own distinct-sample fraction of N (so
      3-assert/2-negate → 0.6 / 0.4, never 1.0), both canonical propositions are
      marked `provisional` (reason: polarity-unstable), and the twin pairing is
      recorded in the extract `Action.outputs` for Trial A5.
- [ ] **Degenerate-sampling guard (review P4):** fail config validation (or warn
      loudly at `Propositionizer` construction) when `LLM_EXTRACT_SAMPLES > 1`
      while sampling temperature is 0 — N greedy samples are (near-)identical and
      agreement is trivially 1.0 while measuring nothing.
- [ ] **Tests (pure, hand-built vectors):** near-identical asserted+negated
      candidates land in separate clusters whose agreements sum to ≤ 1; twin
      detection sets `provisional` on both; same-polarity candidates cluster as
      before; the temperature guard fires.
- [ ] **Ordering note for Trial A5:** land this **before** A5 fits
      `PROP_AGREEMENT_THRESHOLD` — fitting the threshold against the polarity-blind
      clusterer bakes the bug into the calibration.

### G1.15 — Cache key: hash the actual prompt + schema, not a hand-bumped constant (§6.1) *(review A4)*

**Why.** `extraction_content_hash` (`core/cache.py`) discriminates on
`EXTRACT_SCHEMA_VERSION`, a constant whose docstring says "bumped on any prompt /
schema / enum change" — a human-discipline guard on exactly the staleness class
G1.7 was built to close. Edit the prompt, forget the bump, and every cached span
silently replays the old extraction.

- [ ] Add to the hash payload: `prompt_sha` = SHA-256 of the rendered system-prompt
      template (the string `build_messages` interpolates, before per-span
      substitution) and `schema_sha` = SHA-256 of the canonical JSON
      (`sort_keys=True`, compact separators) of the guided-decoding schema. Do the
      same inside the verifier signature (verifier prompt + `VerifyVerdict`
      schema).
- [ ] Keep `schema_version` in the key as a *semantic* version of the output shape;
      it no longer carries invalidation alone.
- [ ] **Tests:** changing one character of the prompt template changes the hash;
      re-ordering schema keys does not; toggling the verifier still does.
- [ ] **Expected effect:** one-time full re-extraction on the next run after this
      lands (the key changes). That is correct and loud, not a regression.

### G1.16 — Embedding-model identity on dense rows + reindex path (§4) *(review A5)*

**Why.** `document_embeddings` / `proposition_embeddings` rows carry no record of
which model produced them. Swap or upgrade the embedding model and the ANN index
becomes a mixed-space soup — cosine across spaces is meaningless — and *nothing can
even detect* the condition.

- [ ] **Migration:** add `model TEXT NOT NULL` to both tables; backfill existing
      rows with `'BAAI/bge-m3'`; include `model` in the table comment as the vector
      space identifier. (Dimension is implicit in the pgvector column; a model
      change that alters dimension fails loudly already — same-dimension swaps are
      the silent case this closes.)
- [ ] **Ingest guard** (mirror `DocumentResegmentationError`): before upserting,
      if rows exist for this document (or proposition set) under a *different*
      `model`, raise `EmbeddingModelMismatchError` — never mix spaces in place.
- [ ] **Reindex path:** `scripts/reembed.py` — for a target model: re-run
      `embed_document`+`pool_span` over each document's raw text to refresh span
      vectors, and `embed_passages` over proposition texts; batched, idempotent
      (skip rows already on the target model), caller owns transaction per batch.
- [ ] **Tests:** mismatch guard raises (integration, ephemeral DB); reembed script
      converges to all-rows-on-target-model and is re-runnable.

### G1.17 — Ingest robustness hardening *(review R1–R8 — one batch PR)*

- [ ] **Per-span error isolation** (`core/proposition.py`): the span-level
      `asyncio.gather` must not let one failing span (or one failing sample) abort
      the document. Use `return_exceptions=True` (or a per-span try), record failed
      spans `(span_id, error)` on the run result, continue, and let the next run
      pick them up via idempotency — the content-addressed cache makes resume
      cheap; lean on it.
- [ ] **Verifier failure = verdict unavailable, not a crash:** a `None`/unparseable
      verifier response leaves `faithfulness`/`provisional` null (the documented
      degraded G1.1 mode) and logs the failure on the verify `Action`, instead of
      surfacing an enum-cast exception mid-batch.
- [ ] **Kill the zero-vector sentinel** (`core/embeddings.py::pool_span`): return
      `None` for no-token spans instead of `[0.0]*hidden`; update callers to skip
      explicitly. Invariant after this: **no zero vector can reach pgvector** (the
      sentinel currently relies on every caller remembering to check — and G1.13's
      truncated spans took this same path).
- [ ] **Action-lookup indexes:** partial functional indexes on
      `actions((inputs->>...), timestamp DESC)` for the parser and segmenter
      idempotency lookups, mirroring migration `0006` (which covered only
      `actor='propositionizer'`). Note in the migration that `actions` is
      append-only and on the hot path of every ingest decision; partitioning is
      deferred until volume warrants.
- [ ] **Overall per-call deadline:** wrap each LLM/verifier call in
      `asyncio.timeout` slightly above the tenacity retry ceiling so a hung
      endpoint cannot hold a semaphore permit through ~5×30 s of backoff and starve
      the batch.
- [ ] **`EmbeddingSubstrate` lifecycle:** add `close()` / context-manager support
      releasing model + tokenizer; document that long-running workers hold one
      instance, not one per document.
- [ ] **`cypher_map` fuzzing** (`db/age.py`): property-based tests (hypothesis)
      round-tripping hostile strings — quotes, backslashes, unicode escapes,
      agtype-syntax fragments — through `cypher_map` → AGE → read-back. Document
      text and LLM output flow through this hand-rolled escaping at an adversarial
      trust boundary; prefer AGE prepared-statement params where the call path
      allows.

### G1.18 — Structured table payload in the parse wire contract (§1 rule a) *(review A1 — do while the wire schema is still on a branch)*

**Why.** §1 promises "tables ingest as structured observations (rows/cells →
propositions with column semantics)". But `ParseResult` is one reading-order text
blob + linear `[start, end)` ranges; a `ParseKind.TABLE` element is just a char
range, so the 2-D structure (rows, headers, cell adjacency) is destroyed at the
trust boundary and Phase 2's table extractor would have nothing to read.
Retrofitting a wire contract after the MinerU service adapter ships is strictly
more work than adding the slot now.

- [ ] Add an optional `table` payload to `ParseElement` (only valid when
      `kind == TABLE`): `{n_rows, n_cols, cells: [{row, col, row_span, col_span,
      is_header, start, end, bbox?}]}` — each cell's `[start, end)` indexes into
      the **same** reading-order blob, so cell provenance still resolves to spans
      and visual provenance still works.
- [ ] **Validation (in `ParseResult.from_offsets` / a table validator):** cell
      offsets lie within the parent element's range; `(row, col)` within bounds; no
      two cells claim the same grid position. Cells need *not* tile the element
      range (separators/whitespace between cells are fine) — do not reuse the
      strict element-tiling rule here.
- [ ] Thread through the wire schema (`mineru.py::_WireResponse`), and persist on
      the span `layout` dict (it is versioned — bump `layout_schema_version`).
- [ ] **Consumer stays Phase 2** (cells → observation-class propositions with
      column semantics). This task only makes the structure *survive Stage 0*.

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
4. **G1.13 slice 1 (truncation guard) + G1.14 (polarity-aware clustering +
   temperature guard)** — ← **next**: small, stop silent corruption; G1.14 must
   precede Trial A5 threshold fitting.
5. **G1.15 (prompt-hash cache key) + G1.16 (embedding-model column)** — two small
   silent-staleness closures; G1.15 triggers one loud full re-extraction.
6. **G1.18 (table payload in wire contract)** — while the MinerU wire schema is
   still on a branch / before the service adapter hardens.
7. **G1.13 slice 2 (windowed embedding)** — before the MinerU service starts
   feeding real multi-page PDFs.
8. **G1.6 quarantine enforcement** — stakes gating (G1.6 flag is set per node;
   edge-time enforcement gated on Phase 2 — make it a Phase 2 *entry* item).
9. **G1.17 robustness batch** — one hardening PR.
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
- [ ] Mixed-polarity extractions can never report full agreement; a
      polarity-unstable span yields `provisional` propositions (G1.14).
- [ ] A prompt-template edit alone invalidates the extraction cache (G1.15); an
      embedding-model swap is refused, not silently mixed (G1.16).
