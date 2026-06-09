# Phase 1 — Ingest Pipeline

**Goal:** turn a document into multi-level spans, decontextualized propositions, and
indexes — knowledge *in*, with source references retained throughout.

**Depends on:** Phase 0 (storage, `Document`/`Span`/`Proposition` schema, provenance).
**Architecture refs:** §1 (parse front-end + embedding substrate), §2 (segmentation
backbone), §3 (proposition layer), §4 (indexing), principles 1–3.

## Document parsing — front-end (§1, Stage 0)

- [ ] **Parser behind a fixed contract** (swappable, like the LLM): input a PDF/scan/doc,
      output reading-order text + structure + tables + located figures + formulas +
      per-element `{page, bbox}`. Default impl: **MinerU**.
- [ ] **Invoke MinerU as a separate hosted service** (CLI/HTTP), **not vendored** — it is
      AGPL-3.0; keep the copyleft at the service edge (§1, licensing track).
- [ ] **Tables → structured observations:** ingest table rows/cells as propositions with
      column semantics preserved (observation-class, §3.1); do not flatten to prose.
- [ ] **Figures located here, interpreted later:** store figure region + caption + bbox;
      a vision `extract` operator (Phase 2/§3) reads propositions off the figure, flagged
      provisional.
- [ ] **Carry `{page, bbox}` into `Span`** for visual provenance (claim → region on the
      original page).
- [ ] **Parse quality = faithfulness input:** mark scanned / handwritten / complex-table
      parses lower-faithfulness → provisional → triage; surface MinerU's span/layout
      visualization for expert QA against the original.

## Embedding substrate (§1)

- [ ] Long-context embedding model run **once** per document; cache contextualized
      token embeddings ("late chunking" — embed once, derive all granularities).
- [ ] Confirm boundary detection, multi-level pooling, and search all read from the
      cached vectors (no per-level re-embedding).

## Segmentation backbone (§2)

- [ ] Adjacent-window similarity signal over the cached embeddings; smooth it.
- [ ] Valley detection by **depth score** + adaptive threshold (mean − k·σ); not raw
      argmin.
- [ ] **DP segmentation** over sentence units: maximize intra-segment coherence minus a
      length penalty (no O(n²) position×size brute force).
- [ ] Length penalty as the **level knob** → multiple abstraction levels (sub-paragraph
      … chapter) from one mechanism; store segments as `Span` offset ranges with
      `level`.
- [ ] Blend an information signal (entity/number density) into the objective so
      segments don't collapse onto redundant blobs.
- [ ] Coarse levels as **summaries**, not just longer windows (RAPTOR-style upward
      tree) — needed so §5.1 coarse-to-fine pruning has crisp parents.

## Proposition layer (§3)

- [ ] Propositionizer: transform sub-paragraph spans into atomic, self-contained
      statements — resolve references, attach qualifiers, split compound claims.
- [ ] Store `Proposition` nodes linked to source `Span`(s) (`EVIDENCED_BY`); never free
      text on other nodes.
- [ ] Emit an `Action` record per propositionization (model, sampling) (§10.1).

## Extraction faithfulness (§3.1) — harden the perception layer

- [ ] **Structured epistemic fields on every proposition** (not flattened into text):
      `polarity` (asserted/negated), `modality` (categorical/probable/possible/
      hypothesized), `attribution` (document/reported-speech/named-source), `scope`, and
      **`epistemic_class`** (observation / testimony / judgement — orthogonal to modality).
- [ ] **Extract observations as facts; do not inherit source conclusions (§3.1, §5).**
      Classify each proposition's `epistemic_class`; ingest a source's *observations* as
      facts and its *conclusions* as defeasible, credibility-weighted judgement-claims —
      never as facts. The engine re-derives conclusions from the observations.
- [ ] **Multi-sample extraction** (reuse §8 calibration): stable extractions →
      high-confidence; unstable → flag.
- [ ] **`verify` step:** entailment/NLI check that the span supports the proposition
      *with its polarity and modality*; disagreement sets `provisional` (§3.1). Prefer an
      **independent verifier (different model family from the extractor)** to reduce
      correlated error (§13).
- [ ] Record a `faithfulness` ∈ [0,1] per proposition — kept **distinct** from source
      credibility (§9) and evidential strength (§8).
- [ ] **Quarantine by stakes:** provisional/low-faithfulness propositions may exist but
      cannot drive high-stakes moves (e.g., a `REFUTES`) until confirmed; route to the
      expert-triage queue.
- [ ] Faithfulness gate metric wired for the trial plan (entailment, negation/modality
      preservation) — see `todo_trials.md` A5.

## Indexing (§4)

- [ ] **Dense** index in pgvector over the chosen granularities.
- [ ] **Sparse/lexical** index (TF-IDF/BM25) — catches names, codes, acronyms.
- [ ] Both indexes carry `box` id so retrieval can be scoped to the active working set.
- [ ] (Keyword/entity index feeds graph nodes in Phase 2 and candidate generation in
      Phase 4 — keep keyworders in the lexical layer, not the graph.)

## Cost & incrementality (§6.1)

- [ ] **Content-addressed cache** for LLM outputs (propositions, extractions) keyed by
      content + model version; unchanged spans are never re-inferred ("extract once").
- [ ] **Amortize reference processing:** reference-corpus / domain-pack boxes are ingested
      **once** and persisted read-only for reuse across investigations; only case
      documents are processed per investigation (§9).

## Exit criteria

- [ ] A document ingests end-to-end: cached embeddings → multi-level spans →
      propositions → dense + sparse indexes, all with retained span references.
- [ ] Hybrid retrieval (dense + sparse), box-scoped, returns propositions with their
      source text resolvable.
- [ ] Re-ingesting an unchanged document hits the cache (no re-extraction); a static
      reference corpus is processed once and reused.
- [ ] Maintain a small fixture corpus exercising this path (seed for the gate corpus).

## Phase risks / decisions

- The DP objective blend (coherence vs information signal) is a knob to tune, not a
  research goal (§8) — don't over-engineer it.
- Summary-based coarse levels add LLM cost at ingest; confirm it's worth it for the
  pruning benefit before scaling.
