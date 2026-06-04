# Implementation Plan: Phase 1, Increment 3 (Proposition Layer)

> Revision 2. Supersedes the first draft after architectural review. Changes from v1 are
> called out inline as **[R2]** so reviewers can see what moved and why.

## 1. Overview and Objectives
Transform sub-paragraph text chunks (`Span` objects) from the `SegmentationBackbone` into
atomic, self-contained factual statements — `Proposition` nodes. Propositions are the
"value layer" (architecture §3): they retrieve better than raw sentences (which break on
unresolved references) and better than passages (too diffuse), and the same
decontextualization that makes a span retrievable makes it graph-ready for Phase 2 fact
extraction.

### Key Requirements
- **Decontextualization** (§3): resolve pronouns to their referents, attach qualifiers,
  split compound sentences into independent claims.
- **Context-aware extraction** (§1) **[R2]**: referents usually live *outside* the target
  span ("He" → "Smith" comes from earlier text). The LLM is given surrounding document
  context as *input*, while only *emitting* propositions for the target span. Span-only
  context is insufficient and is explicitly rejected — see §2.1 and §4.
- **Traceability** (principle 4, §10): every proposition links to its source `Span`(s) via
  an `EVIDENCED_BY` edge; every run records `Action` rows with concrete id lists (§10.2).
- **Reliability** (§8, principle 6): structured output is guaranteed at decode time, calls
  are bounded and retryable, and writes are idempotent per span.
- **Indexing** (§4): propositions are indexed densely (pgvector) and sparsely (lexical-exact
  for names/codes/acronyms) for hybrid search.

### Non-goals (thin-slice discipline, todo.md "build philosophy")
- No multi-sample / calibration regime for propositionization. The §8 multi-sample
  disciplines target *edge judgment* (sign + strength), not extraction. Propositionization
  is a single guided-decode call. **[R2]** — stated explicitly to avoid gold-plating.
- No `box` assignment logic yet (Phase 2 owns boxing/tiers). See §2.2 for the deferral.
- No query-time retrieval/ranking. We build the indexes; querying them is a later increment.

---

## 2. Architecture & Components

### 2.1 The `Propositionizer` Module (`src/iknos/core/proposition.py`)
Core orchestrator for the increment.

- **Input**: the ordered `list[Span]` for a single `Document`, plus the document's raw text
  (already in `document_content.raw_text`).
- **Output**: `Proposition` objects, each mapped to its source span id(s).

**Context regime [R2] (resolves the v1 O(N)/decontextualization contradiction).**
Decontextualization is impossible from the span alone. For each target span we build a
prompt containing:
  - a **leading context window** — the preceding *K* spans of the same document (default
    `K = 8`, configurable), rendered as plain text, and
  - the **target span**, clearly delimited.
The system prompt instructs the model: *use the context only to resolve references; emit
propositions solely for claims asserted in the target span.* This keeps the **number of LLM
calls** linear in spans (one call per span) while being honest that **per-call token cost**
grows with the window — the cost model is `O(N · K)` tokens, not `O(N)`. `K` is the tuning
knob between cost and reference-resolution quality. (A future optimization — a running
coref/entity state instead of raw preceding text — is noted but out of scope.)

- **LLM integration**: calls the local vLLM OpenAI-compatible endpoint via the new
  `src/iknos/core/llm.py` client (§2.4). Structured output is enforced with vLLM **native
  guided decoding** (`extra_body={"guided_json": <schema>}`), a grammar-level guarantee —
  not reprompt-and-validate. **[R2]** — replaces v1's `instructor`/`outlines` choice; we use
  what the inference server already provides, consistent with "build, not buy" (principle 7)
  and the reliability goal (§8).

- **Concurrency model**: `asyncio.gather` over spans bounded by an `asyncio.Semaphore`
  (default 8) so we saturate vLLM without exhausting it (backpressure). LLM and embedding
  work happen **outside any DB transaction** (§2.5). `tenacity` retry (exponential backoff)
  wraps the LLM call for transient network/5xx errors only — *not* for JSON-shape errors,
  which guided decoding makes unreachable.

### 2.2 Data Modeling — Graph (`src/iknos/types/nodes.py`, AGE)
The `Proposition` AGE vertex label is pre-created (migration `0001`). Add the Pydantic
projection, matching the schema in architecture §10 **[R2]**:
```python
class Proposition(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: uuid.UUID
    text: str            # the rewritten, self-contained form
    # box: omitted for the thin slice — Phase 2 owns boxing/tiers. Tracked deviation.
```
Notes:
- **Drop `document_id`** from the node **[R2]**. Provenance is *always* a `Span` id, never a
  ref embedded on another node (§10); `document_id` is derivable via
  `EVIDENCED_BY → Span → document_id`. (If per-document partitioning later proves necessary
  it can be reconsidered, but it is not needed now.)
- `box` is intentionally deferred and recorded here as a conscious deviation from the §10
  schema, not an oversight.

**Edges.** Insert `(p:Proposition)-[:EVIDENCED_BY]->(s:Span)` for the source span. When a
proposition's referents were resolved from one or more context spans, also emit
`EVIDENCED_BY` to those spans **[R2]** — provenance is "the Span(s) it came from" (§10), and
the resolving span is genuinely part of the evidence. The LLM is asked to return, per
proposition, the set of span ids it relied on (target + any context spans); we validate
those ids against the window before writing.

### 2.3 Data Modeling — Relational & Indexes (`src/iknos/db/orm.py`, Alembic)

**Dense index — new `proposition_embeddings` table [R2].**
We do **not** overload `DocumentEmbedding`. That table's `span_start`/`span_end`/`level`
columns are document-offset semantics that are meaningless for rewritten proposition text;
a nullable `proposition_id` would produce a half-NULL, two-meanings table. A dedicated table
is cleaner and scales independently:
```python
class PropositionEmbedding(Base):
    __tablename__ = "proposition_embeddings"
    id:             Mapped[uuid.UUID]  = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    proposition_id: Mapped[uuid.UUID]  = mapped_column(index=True)      # AGE node id (no cross-store FK)
    document_id:    Mapped[uuid.UUID]  = mapped_column(ForeignKey("document_content.document_id", ondelete="CASCADE"), index=True)
    embedding:      Mapped[list[float]] = mapped_column(Vector(1024))
```
An IVFFlat/HNSW index on `embedding` is deferred until we have row volume to tune it
(noted, not silently skipped).

**Sparse index — lexical-exact, not "BM25" [R2].**
v1 conflated `tsvector` with BM25; Postgres FTS stems and strips stop-words by default, which
*degrades* the §4 goal of exact recall on names, codes, and acronyms (`AB-1234` must survive).
For the thin slice we use a `tsvector` column built with the **`simple`** (unstemmed,
no-stopword) configuration, and we name it for what it is — lexical-exact match, not BM25
ranking:
```python
class PropositionLexicalIndex(Base):
    __tablename__ = "proposition_lexical_index"
    proposition_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    document_id:    Mapped[uuid.UUID] = mapped_column(ForeignKey("document_content.document_id", ondelete="CASCADE"), index=True)
    lexemes:        Mapped[str]       = mapped_column(TSVECTOR)   # to_tsvector('simple', text)
    # GIN index on lexemes
```
True BM25 ranking (via `pg_search`/ParadeDB or a hand-rolled term table, per "build not buy")
is recorded as the scale path for when ranking quality, not just recall, is needed.

**Action logging — id lists, per span [R2].**
v1's "input/output counts" breaks point-auditability: §10.2 requires joining a node to the
`Action` that produced it *by output id*. We record one `Action` **per span** (not one opaque
batch row) via the existing `record_action()`:
- `actor = "propositionizer"`, `action_type = "extract"`
- `inputs = {"target_span": <id>, "context_spans": [<id>...]}`
- `outputs = {"propositions": [<id>...], "edges": [<id>...]}`
- `model`, `sampling` (temperature/top_p/seed/window K), and `raw_judgment` (the raw LLM JSON)
This makes every proposition answer "which action produced me" by `outputs` join.

### 2.4 LLM Client (`src/iknos/core/llm.py`) — new
Thin async wrapper over the `openai` client (already a dependency) pointed at the vLLM
endpoint (`base_url` from settings; default `http://192.168.0.247.../v1`).
- One method: `async def guided_complete(messages, json_schema, sampling) -> dict`.
- Uses `extra_body={"guided_json": json_schema}`.
- `tenacity` retry on connection/5xx only.
- Extraction schema: `class PropositionExtraction(BaseModel): propositions: list[PropositionOut]`
  where `PropositionOut` carries `text: str` and `evidence_span_ids: list[uuid.UUID]`.

### 2.5 Embedding Integration (`src/iknos/core/embeddings.py`) — new capability [R2]
Propositions are **rewritten strings that do not appear in the document**, so they cannot be
pooled from the cached late-chunking token embeddings (`DocumentContext.pool_span` works by
character offset). This is a *separate embedding regime* and we say so. Add a first-class,
batched passage-embedding method to `EmbeddingSubstrate`:
```python
def embed_passages(self, texts: list[str]) -> list[list[float]]:
    """Encode standalone short texts to one normalized 1024-d vector each (real batching:
    one padded tokenizer call + one forward pass). Distinct from embed_document/pool_span,
    which derive span vectors from cached document context."""
```
Pooling = mean over real tokens (mask-aware) + L2 normalize, matching `pool_span`'s
convention so dense vectors are comparable across spans and propositions.

### 2.6 Transaction & idempotency boundary [R2]
**Pipeline ordering (per span):**
`load context → LLM guided_complete → embed_passages → short write transaction`.
All slow GPU/LLM work is outside the transaction; we never hold an `AsyncSession`
transaction open across an inference call (which would exhaust the async pool under the
semaphore fan-out).

**Idempotency — designed, not asserted.** Before writing a span, check whether it has already
been propositionized: query AGE for an existing `(:Proposition)-[:EVIDENCED_BY]->(s:Span {id})`
edge (or, equivalently, an `Action` with `inputs.target_span == id`). If present, skip
(default) — making re-runs and crash-resume safe and duplicate-free. The per-span write is a
single transaction (proposition nodes + edges + dense row + lexical row + `Action`), so a
crash leaves a span either fully done or untouched. Re-propositionization semantics
(replace vs append) default to **skip-if-present**; a `--force` replace path is noted but out
of scope.

---

## 3. Step-by-Step Execution Plan

1. **Types & schema**
   - Add `Proposition` to `nodes.py` (§2.2).
   - Add `PropositionEmbedding` and `PropositionLexicalIndex` to `orm.py` (§2.3).
   - Hand-write an Alembic migration (`..._0003_proposition_layer.py`) — both tables, the GIN
     index on `lexemes`, `proposition_id`/`document_id` indexes. Follow the `0001`/`0002`
     conventions (relational DDL under `public`; no AGE label changes needed — `Proposition`
     already exists). Autogenerate is *not* used.

2. **LLM client** (`src/iknos/core/llm.py`, §2.4) — async, guided decoding, scoped retry.

3. **Embedding capability** (`embed_passages`, §2.5) — with its own unit test.

4. **Propositionizer** (`src/iknos/core/proposition.py`, §2.1)
   - System prompt: decontextualization rules + "context resolves references, emit only
     target-span claims" + return `evidence_span_ids`.
   - Context-window assembly, semaphore-bounded `gather`, tenacity wrap.
   - Per-span pipeline with the transaction/idempotency boundary of §2.6.

5. **Persistence**
   - Cypher to insert `Proposition` nodes and `EVIDENCED_BY` edges (reuse
     `execute_cypher`/`cypher`; build query text safely as in existing tests).
   - Insert dense + lexical rows; `record_action()` per span (§2.3).
   - One `AsyncSession` transaction per span.

6. **Add dependency**: `tenacity` in `pyproject.toml` (`instructor`/`outlines` **not** added —
   guided decoding lives in vLLM). `openai` already present.

---

## 4. Scalability, Robustness & Reliability Guarantees

- **Cost model, stated honestly [R2]**: one LLM call per span → call count is `O(N)`; token
  cost is `O(N · K)` because of the context window `K`. `K` is the explicit knob trading cost
  for reference-resolution quality. No `O(N²)` whole-document attention.
- **Backpressure**: `asyncio.Semaphore` bounds concurrent vLLM calls; no self-inflicted DDoS.
- **No long-held transactions [R2]**: inference/embedding run outside the DB transaction; the
  write tx is milliseconds (§2.6).
- **Idempotency by construction [R2]**: skip-if-evidenced check + per-span atomic write means
  retries (tenacity) and crash-resume never duplicate nodes/edges.
- **Guaranteed structured output [R2]**: grammar-constrained decoding, so malformed JSON is
  not a runtime failure mode.
- **Point-auditability preserved [R2]**: per-span `Action` rows with concrete input/output id
  lists satisfy §10.2 (join a proposition to its producing action by output id).
- **Provenance completeness [R2]**: `EVIDENCED_BY` to the target span *and* any context spans
  used for reference resolution (§10).

---

## 5. Testing (TDD)

**Unit (`tests/unit/test_proposition.py`, mocked LLM — matches existing convention):**
- Context-window assembly: target span + preceding `K` spans, boundaries correct, K=0 and
  start-of-document edge cases.
- Structural handling: mocked guided output → correct `Proposition` objects and the intended
  `EVIDENCED_BY` edge set (target + declared context spans); invalid `evidence_span_ids`
  (outside the window) are rejected.
- Idempotency: a span already carrying an `EVIDENCED_BY`-from-Proposition is skipped.
- Retry: tenacity retries a simulated 5xx but does **not** retry a schema error path.
- `embed_passages` (`tests/unit/test_embeddings.py`): batch in → N normalized 1024-d vectors;
  unit-norm; independent of batch position/padding.

**Integration (`tests/integration/`, real Postgres+AGE, fixture corpus):**
- End-to-end on a small fixture document: spans → propositions → `EVIDENCED_BY` edges →
  dense rows → lexical rows → per-span `Action` rows, all in one process. Verify a
  proposition is walkable Proposition → Span → source text, and joinable to its `Action` by
  output id.
- Tie this fixture into the **Phase 1 exit criteria** suite (maintained corpus), not an
  isolated test.
- Lexical-exact check: a planted code/acronym (e.g. `AB-1234`) is recoverable from the
  `simple`-config `tsvector` (guards against the stemming regression that motivated §2.3).

---

## 6. Tracked deviations from architecture §10 (for Phase 2 reviewers)
- `Proposition.box` omitted (Phase 2 owns boxing). 
- `proposition_id`/`document_id` carried in relational tables only as query keys; AGE remains
  the source of truth for nodes/edges (consistent with `orm.py`'s existing comment that AGE
  schema is not ORM-modeled).
- BM25 ranking deferred; lexical-exact recall shipped now.
