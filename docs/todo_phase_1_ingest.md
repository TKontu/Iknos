# Phase 1 — Ingest Pipeline

**Goal:** turn a document into multi-level spans, decontextualized propositions, and
indexes — knowledge *in*, with source references retained throughout.

**Depends on:** Phase 0 (storage, `Document`/`Span`/`Proposition` schema, provenance).
**Architecture refs:** §1 (embedding substrate), §2 (segmentation backbone), §3
(proposition layer), §4 (indexing), principles 1–3.

## Embedding substrate (§1)

- [x] Long-context embedding model run **once** per document; cache contextualized
      token embeddings ("late chunking" — embed once, derive all granularities). *(Implemented `EmbeddingSubstrate` locally using PyTorch/Transformers to handle `bge-m3` token-level embeddings.)*
- [x] Confirm boundary detection, multi-level pooling, and search all read from the
      cached vectors (no per-level re-embedding). *(Confirmed. `SegmentationBackbone` exclusively uses `context.pool_span` to chunk the document without any re-embedding.)*

## Segmentation backbone (§2)

- [x] Adjacent-window similarity signal over the cached embeddings; smooth it. *(Implemented `calculate_adjacent_similarities` and `smooth_similarities`.)*
- [x] Valley detection by **depth score** + adaptive threshold (mean − k·σ); not raw
      argmin. *(Implemented `find_valleys` filtering candidates by standard deviation.)*
- [x] **DP segmentation** over sentence units: maximize intra-segment coherence minus a
      length penalty (no O(n²) position×size brute force). *(Implemented `segment_dp` using $O(1)$ PyTorch prefix sums to completely avoid O(N²) scaling.)*
- [ ] Length penalty as the **level knob** → multiple abstraction levels (sub-paragraph
      … chapter) from one mechanism; store segments as `Span` offset ranges with
      `level`.
- [x] Blend an information signal (entity/number density) into the objective so
      segments don't collapse onto redundant blobs. *(Implemented `calculate_information_density` using fast regex heuristics for numbers, symbols, and entities.)*
- [ ] Coarse levels as **summaries**, not just longer windows (RAPTOR-style upward
      tree) — needed so §5.1 coarse-to-fine pruning has crisp parents.

## Proposition layer (§3)

*(Increment 3, done — see `docs/proposition_layer_plan.md` for the reviewed design. LLM
calls go to the local vLLM endpoint; structured output via native `guided_json`; runs are
idempotent per span.)*

- [x] Propositionizer: transform sub-paragraph spans into atomic, self-contained
      statements — resolve pronouns, attach qualifiers, split compound claims. *(Implemented
      `Propositionizer` (`core/proposition.py`) + async vLLM client (`core/llm.py`). Each
      span is decontextualized with a **preceding-K-span context window** so references
      resolve (cost is O(N) calls / O(N·K) tokens — span-only context was rejected per §1).
      3-phase run: idempotency filter → semaphore-bounded concurrent inference → serial
      per-span commit, so the shared session is never used concurrently and LLM/embedding
      work stays outside the write transaction.)*
- [x] Store `Proposition` nodes linked to source `Span`(s) (`EVIDENCED_BY`); never free
      text on other nodes. *(`Proposition` Pydantic model added; nodes + `EVIDENCED_BY`
      edges written via Cypher (`cypher_map` helper promoted into `db/age.py`).
      **Scoped:** provenance links to the **target span only**; consulted context-span ids
      are recorded in `Action.inputs` for audit, not as edges (multi-span provenance is a
      deferred refinement). Assumes `Span` vertices already exist in AGE — see follow-up.)*
- [x] Emit an `Action` record per propositionization (model, sampling) (§10.1). *(One
      `Action` per span with concrete id lists — `inputs.target_span`/`context_spans`,
      `outputs.propositions`/`edges` — so any proposition joins back to its action by
      output id (§10.2). Action-based idempotency also covers zero-proposition spans.)*

## Indexing (§4)

- [ ] **Partial.** **Dense** index in pgvector over the chosen granularities. *(Two stores
      now exist: `document_embeddings` (`VECTOR(1024)`, Alembic `0002`) for span granularities
      — **population still pending**; and `proposition_embeddings` (Alembic `0003`) which **is
      populated** during propositionization via a new batched `EmbeddingSubstrate.embed_passages`
      (propositions are rewritten text, so they're embedded afresh, not pooled from the cached
      late-chunking vectors).)*
- [x] **Sparse/lexical** index (TF-IDF/BM25) — catches names, codes, acronyms. *(Implemented
      `proposition_lexical_index` (Alembic `0003`) — Postgres `tsvector` built with the
      **`simple`** config (unstemmed, no stop-words) + **GIN** index, so codes/acronyms like
      `AB-1234` survive verbatim for exact recall. This is lexical-exact, **not** BM25 ranking;
      true BM25 (pg_search/ParadeDB) is the noted scale path. Populated during propositionization.)*
- [ ] Both indexes carry `box` id so retrieval can be scoped to the active working set.
      *(Deferred: `box` is owned by Phase 2; the proposition indexes carry `document_id` for
      now, `box` added when boxing lands.)*
- [ ] (Keyword/entity index feeds graph nodes in Phase 2 and candidate generation in
      Phase 4 — keep keyworders in the lexical layer, not the graph.)

## Follow-ups surfaced (Increment 3)

- [ ] **Persist `Span` vertices (and span-level `document_embeddings` rows) from the
      segmentation output.** Currently `segment_document` returns in-memory `(start, end)`
      tuples and nothing writes `Span` nodes to AGE or populates the dense span index — so the
      Propositionizer assumes spans already exist and the integration test creates them by
      hand. This is the **blocker for true end-to-end ingest** and the natural next increment.
- [ ] Optional: multi-span provenance — add `EVIDENCED_BY` to the context spans a proposition
      drew on for reference resolution (today: target span only; context ids are in
      `Action.inputs`).
- [ ] Add `box` to the proposition dense/sparse indexes once Phase 2 boxing lands.

## Exit criteria

- [ ] A document ingests end-to-end: cached embeddings → multi-level spans →
      propositions → dense + sparse indexes, all with retained span references.
      *(Blocked on span persistence above; the proposition→index half is built and tested.)*
- [ ] Hybrid retrieval (dense + sparse), box-scoped, returns propositions with their
      source text resolvable. *(Indexes now exist; the query/ranking layer is a later increment.)*
- [ ] Maintain a small fixture corpus exercising this path (seed for the gate corpus).
      *(Started: `tests/integration/test_proposition_layer.py` exercises span→proposition→
      both indexes→Action with mocked LLM/embeddings.)*

## Phase risks / decisions

- The DP objective blend (coherence vs information signal) is a knob to tune, not a
  research goal (§8) — don't over-engineer it.
- Summary-based coarse levels add LLM cost at ingest; confirm it's worth it for the
  pruning benefit before scaling.
