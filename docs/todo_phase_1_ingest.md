# Phase 1 — Ingest Pipeline

**Goal:** turn a document into multi-level spans, decontextualized propositions, and
indexes — knowledge *in*, with source references retained throughout.

**Depends on:** Phase 0 (storage, `Document`/`Span`/`Proposition` schema, provenance).
**Architecture refs:** §1 (embedding substrate), §2 (segmentation backbone), §3
(proposition layer), §4 (indexing), principles 1–3.

## Embedding substrate (§1)

- [x] Long-context embedding model run **once** per document; cache contextualized
      token embeddings ("late chunking" — embed once, derive all granularities). *(Implemented `EmbeddingSubstrate` locally using PyTorch/Transformers to handle `bge-m3` token-level embeddings.)*
- [ ] Confirm boundary detection, multi-level pooling, and search all read from the
      cached vectors (no per-level re-embedding). *(Pooling logic implemented in `DocumentContext`, ready for the segmentation backbone to consume it.)*

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
      statements — resolve pronouns, attach qualifiers, split compound claims.
- [ ] Store `Proposition` nodes linked to source `Span`(s) (`EVIDENCED_BY`); never free
      text on other nodes.
- [ ] Emit an `Action` record per propositionization (model, sampling) (§10.1).

## Indexing (§4)

- [ ] **Partial.** **Dense** index in pgvector over the chosen granularities. *(Schema `document_embeddings` with `VECTOR(1024)` created in Alembic `0002` migration; index population pending.)*
- [ ] **Sparse/lexical** index (TF-IDF/BM25) — catches names, codes, acronyms.
- [ ] Both indexes carry `box` id so retrieval can be scoped to the active working set.
- [ ] (Keyword/entity index feeds graph nodes in Phase 2 and candidate generation in
      Phase 4 — keep keyworders in the lexical layer, not the graph.)

## Exit criteria

- [ ] A document ingests end-to-end: cached embeddings → multi-level spans →
      propositions → dense + sparse indexes, all with retained span references.
- [ ] Hybrid retrieval (dense + sparse), box-scoped, returns propositions with their
      source text resolvable.
- [ ] Maintain a small fixture corpus exercising this path (seed for the gate corpus).

## Phase risks / decisions

- The DP objective blend (coherence vs information signal) is a knob to tune, not a
  research goal (§8) — don't over-engineer it.
- Summary-based coarse levels add LLM cost at ingest; confirm it's worth it for the
  pruning benefit before scaling.
