# Gap Plan — Review Remediation (June 2026)

**Why this file exists.** The June 2026 architecture/plan review
(`review_2026-06_architecture_and_plan.md`) found fault-leading defects in shipped
code, performance assumptions not backed by schema/code, and plan-sequencing
mistakes. This file holds the **implementation tasks**. Each task is written to be
executable by an agent without further context: read only the files named in the
task, follow the spec, satisfy the acceptance criteria, add the named tests.

**Reconciliation with PR #30.** A parallel review pass merged as #30
(`review_2026-06_architecture_plan.md`, tasks G1.13–G1.19 + G0.R2) overlaps part
of this file. **Where a task overlaps a #30 item, the #30 item governs:** R1/R2 ≈
G1.13 (long-document windowing), R5 ≈ G0.R2 (AGE property indexes), R7 ≈ G1.16
(embedding-model identity), R6 ≈ G1.17's per-call deadline, the RRF fusion
decision ≈ G1.19. The tasks **unique to this file** — not covered by #30 — are:
**R3** (Cypher dollar-quote injection — distinct from G1.17's `cypher_map`
fuzzing: the defect is the `$$` delimiter in `cypher()` itself), **R4** (HNSW ANN
indexes on the pgvector tables — #30 indexed AGE, not pgvector), **R8**
(`provisional_reasons`), **R9** (quarantine gate function), **R10** (embeddings
out-of-process), **R11** (job queue / single-writer-per-box concurrency), **R12**
(Action metrics), **R13** (Phase 2 design docs: entity linking, meronymy
induction, credibility derivation). The architecture *decisions* referenced by
these tasks (§6 concurrency model + dollar-tag write-path rule, §7.4 supersession
triggers, §9.1 eager sensitivity-propagation timing, §3.1/§10
`provisional_reasons`, §4 HNSW/cosine contract, Trial E0 — the E1-lite
perception-layer kill-switch at Phase 2 exit) are specified inside the task bodies
below; folding them into `architecture.md`/`todo_trials.md` prose is part of
landing each task.

**Conventions for executing agents (read first):**

- Run everything via the project venv: `.venv/bin/python` / `uv run` — never bare
  `python3`.
- Tests: `uv run pytest tests/unit -x` for unit; integration tests need the
  ephemeral DB (see `MIGRATIONS.md`); if you cannot run integration tests, say so —
  do not claim them green.
- Do not start Docker containers without explicit approval.
- One task per PR. Branch name `fix/r<N>-<slug>`. Reference this file and the task
  id in the PR body.
- `architecture.md` is the source of truth; if a task seems to contradict it,
  stop and report instead of improvising.
- After ruff autofix, re-check that imports you added are still present (the
  PostToolUse hook can strip an import whose use lands in a later edit).

## Status / sequencing

| Task | Title | Severity | Gate | Status |
|------|-------|----------|------|--------|
| R1 | Truncation guard + zero-vector discrimination | Critical | merge ASAP | ☐ |
| R2 | Windowed late chunking (long documents) | Critical | merge ASAP (after R1) | ☐ |
| R3 | Cypher dollar-quote hardening | Critical | merge ASAP | ☐ |
| R4 | HNSW indexes + distance-operator standardization | High | Phase 2 entry | ☐ |
| R5 | AGE property-id expression indexes | High | Phase 2 entry | ☐ |
| R6 | Explicit LLM client timeouts | Medium | merge ASAP | ☐ |
| R7 | `model_version` on embedding tables | Medium | Phase 2 entry | ☐ |
| R8 | `provisional` → `provisional_reasons` | Medium | before Phase 2 edge work | ☐ |
| R9 | G1.6 quarantine gate function | Medium | before Phase 2 edge work | ☐ |
| R10 | Embeddings served out-of-process | Medium | Phase 2 entry | ☐ |
| R11 | Background job queue (procrastinate) | Medium | Phase 2 entry | ☐ |
| R12 | Action metrics (observability floor) | Medium | opportunistic | ☐ |
| R13 | Phase 2 prerequisite design docs | High | Phase 2 entry | ☐ |

R1+R3+R6 are independent and can run in parallel. R2 depends on R1 (it replaces
R1's hard failure with real support). R8 precedes R9. R4/R5/R7 are independent
migrations (coordinate revision ids: each task must set `down_revision` to the
current head at the time it lands — check `alembic heads` first).

---

## R1 — Fail loudly on embedding truncation; stop conflating zero-vector spans with whitespace

**Severity: Critical (silent data loss).**

**Context.** `src/iknos/core/embeddings.py::EmbeddingSubstrate.embed_document`
tokenizes with `truncation=True, max_length=8192`. For documents longer than 8,192
tokens, spans past the truncation point get no overlapping tokens in
`DocumentContext.pool_span`, which returns a zero vector (`embeddings.py:40-42`).
`src/iknos/core/ingest.py::persist_spans` then silently drops zero-vector spans
(they are counted in `PersistResult.skipped` together with whitespace spans, see
`_is_zero_vector` and the `skipped` field). Net effect: the tail of any long
document is silently never ingested. Until R2 lands, a too-long document must be a
**loud error**, never a partial silent success.

**Changes.**

1. In `core/embeddings.py`:
   - Add at module level:
     ```python
     class DocumentTooLongError(Exception):
         """Document exceeds the embedding context window; windowed late chunking
         (gap_review_2026-06.md R2) is required to ingest it. Raised instead of
         silently truncating, which would drop all content past the window."""
     ```
   - In `embed_document`, detect truncation and raise. Detection: tokenize, then
     compare the *last* offset in `offset_mapping` (the max `tok_end` over non-special
     tokens) against `len(text)`. If the covered character range ends more than a
     small slack (e.g. 16 chars of trailing whitespace) before `len(text.rstrip())`,
     raise `DocumentTooLongError` with a message that includes the document character
     length and the covered character count. Do **not** rely on token counting a
     second time (one tokenizer pass only).
2. In `core/ingest.py`:
   - `persist_spans` currently treats zero-vector spans and whitespace spans as one
     `skipped` bucket. Split them: a **whitespace-only span** (its text is empty after
     `.strip()`) is a legitimate skip; a **non-whitespace span whose pooled embedding
     is a zero vector** indicates substrate corruption and must raise a new
     `EmbeddingCoverageError` (define it in `core/ingest.py`) naming the span's
     `(start, end)`. After R1, this should be unreachable (truncation already raised),
     but it is the guard that keeps the invariant honest if a future substrate change
     reintroduces partial coverage.
   - Keep `PersistResult.skipped` counting whitespace skips only; update its
     docstring/comment accordingly.

**Acceptance criteria.**
- [ ] Ingesting a document longer than the embedding window raises
      `DocumentTooLongError` before any DB write.
- [ ] A whitespace-only span is still skipped silently (existing behaviour).
- [ ] A non-whitespace span with a zero-vector embedding raises
      `EmbeddingCoverageError`.
- [ ] No change to behaviour for documents within the window (existing tests pass).

**Tests** (in `tests/unit/test_embeddings.py` and `tests/unit/test_ingest.py`):
- A text engineered to exceed 8,192 tokens (e.g. `"word " * 20000`) →
  `embed_document` raises `DocumentTooLongError`.
- A short text → no raise; result unchanged vs current behaviour.
- `persist_spans`-level: feed a fake span set where one non-whitespace span has a
  zero vector → `EmbeddingCoverageError`; where a whitespace span has a zero
  vector → skipped, counted, no error. (Use the existing unit-test seams; do not
  require a DB — if `persist_spans` cannot be unit-tested without a DB, put the
  check in a pure helper, e.g. `classify_span_embedding(text, vector) ->
  Literal["ok","skip_whitespace","error"]`, test that, and call it from
  `persist_spans`.)

**Do not:** implement windowing here (that is R2); change the 8192 constant; touch
`embed_passages`.

---

## R2 — Windowed late chunking: long-document support for the embedding substrate

**Severity: Critical (restores capability removed by R1's guard).**

**Context.** `architecture.md` §1 (amended June 2026) specifies **windowed late
chunking**: documents longer than the embedding context are embedded in overlapping
macro-windows and the token embeddings are stitched, so every span in a document of
any length pools from contextualized embeddings. This replaces R1's hard failure
for long documents.

**Spec (from §1).**
- Window size `W` = model max (8,192 tokens). Overlap `V` = 2,048 tokens
  (stride = `W − V` = 6,144). Both configurable via `config.py` settings
  (`EMBED_WINDOW_TOKENS`, `EMBED_WINDOW_OVERLAP_TOKENS`) with these defaults.
- Tokenize the document **once** without truncation (`truncation=False`,
  `return_offsets_mapping=True`); slice the token sequence into windows on token
  boundaries; run the model per window (each window's input must include the
  model's special tokens — re-encode per window from the window's character slice,
  or build input ids per window with special tokens added; pick the simpler
  correct option for the tokenizer in use and document the choice in the
  docstring).
- **Stitching rule:** for a token covered by two windows, take its embedding from
  the window in which it is **more interior** (greater distance to its window's
  nearest edge, in tokens; ties → earlier window). The result is one embedding per
  document token plus the document-level `offset_mapping`, exactly the
  `DocumentContext` shape that exists today.
- A document that fits in one window must produce **bit-identical results to the
  current single-pass path** (regression guarantee).
- Fold `EMBED_WINDOW_TOKENS`/`EMBED_WINDOW_OVERLAP_TOKENS` into the segmentation
  content hash (`core/ingest.py::span_content_hash` takes `segmenter_params` /
  model identity — add the window params to the hashed dict), so changing window
  parameters trips the `DocumentResegmentationError` guard rather than silently
  mixing substrates.

**Changes.** `core/embeddings.py` (`embed_document` grows the windowed path;
`DocumentTooLongError` from R1 becomes unreachable for any finite document but
stays defined), `core/ingest.py` (hash params), `src/iknos/config.py` (settings).

**Acceptance criteria.**
- [ ] A 25,000-token document embeds end-to-end; every non-whitespace span in its
      tail pools a non-zero vector.
- [ ] A short document produces identical output to the pre-change code (assert
      vector equality on a fixture).
- [ ] Changing `EMBED_WINDOW_TOKENS` changes the segmentation content hash.
- [ ] Memory note: windows are processed sequentially and accumulated on CPU
      (`.cpu()` per window) — do not hold all windows on GPU.

**Tests** (`tests/unit/test_embeddings.py`):
- Stitching unit test with a small fake "model" (monkeypatched forward returning
  deterministic per-position vectors): verify interior-token selection at the
  overlap, verify token count == document token count, verify offsets are
  document-global.
- Window-boundary span: a span straddling the stitch seam pools from tokens of
  both windows without error.
- Single-window regression equality test.

**Do not:** persist token embeddings (the architecture explicitly keeps them
transient — §1); change pooling or normalization; add new dependencies.

---

## R3 — Harden the AGE Cypher write path against `$$` in document-derived text

**Severity: Critical (SQL injection / ingest breakage).**

**Context.** `src/iknos/db/age.py::cypher` wraps the Cypher body in PostgreSQL
dollar-quoting: `SELECT * FROM cypher('<graph>', $$ <body> $$) AS (…)`.
Document-derived text is inlined into that body (proposition text at
`core/proposition.py:426-434`, document title at `core/ingest.py:373-374`, span
layout JSON at `core/ingest.py:278-279`). `cypher_map` escapes quotes and
backslashes but nothing protects the `$$` delimiter: any value containing `$$`
terminates the dollar-quoted string and the remainder is executed as raw SQL.

**Changes** (all in `src/iknos/db/age.py`):

1. Replace the fixed `$$` delimiter with a **per-statement unique dollar tag**:
   ```python
   def _dollar_tag(body: str) -> str:
       """Return a dollar-quote tag guaranteed absent from body."""
       i = 0
       tag = "$iknos$"
       while tag in body:
           i += 1
           tag = f"$iknos{i}$"
       return tag
   ```
   In `cypher()`, compute `tag = _dollar_tag(query)` and emit
   `SELECT * FROM cypher('<graph>', {tag} {query} {tag}) AS ({returns})`.
   (Deterministic, no randomness — keeps statements reproducible/cacheable.)
2. `settings.graph_name` is interpolated inside single quotes in `cypher()`;
   assert it matches `^[A-Za-z_][A-Za-z0-9_]*$` at use (raise `ValueError`
   otherwise) so a misconfigured graph name cannot break the SQL string.
3. Extend the module docstring: values are escaped by `cypher_map`, the body is
   protected by a unique dollar tag, keys/graph names are trusted-only and
   validated.

**Acceptance criteria.**
- [ ] A proposition / document title / layout value containing `$$`, `$iknos$`,
      single quotes, backslashes, and newlines round-trips through
      `cypher_map` + `cypher` + `execute_cypher` without SQL error and reads back
      byte-identical.
- [ ] All existing call sites work unchanged (no signature changes besides the
      internals of `cypher()`).

**Tests:**
- Unit (`tests/unit/test_age.py`, new file): `_dollar_tag` returns `$iknos$` for a
  clean body, escalates for a body containing `$iknos$`; `cypher()` output contains
  the tag twice and never a bare `$$` wrapping; graph-name validation raises on
  `bad-name; DROP`.
- Integration (`tests/integration/test_span_persistence.py`, add one test): ingest
  a document whose text contains `payment of $$120 and $tag$ markers 'quoted'
  \backslash` end-to-end; assert spans/propositions persist and the text reads
  back exactly.

**Do not:** attempt AGE prepared-statement parameters in this task (a larger
refactor; the tag fix is complete on its own); URL-encode or mutate stored text.

---

## R4 — ANN (HNSW) indexes on both vector tables + distance-operator standardization

**Severity: High (silent performance collapse at corpus scale).**

**Context.** `document_embeddings` (migration `0002`) and `proposition_embeddings`
(migration `0003`) have no ANN index; every k-NN is a sequential scan.
`architecture.md` §4/§5.1 (amended June 2026) standardizes on **cosine distance**
(`vector_cosine_ops`; vectors are L2-normalized so cosine ≡ inner product — cosine
chosen for robustness if normalization ever drifts) and requires an HNSW index on
every vector column.

**Changes.**
1. New alembic migration (next free revision id; `down_revision` = current head —
   check `alembic heads`), named `0007_hnsw_vector_indexes` (adjust the number to
   the actual next slot):
   ```sql
   CREATE INDEX ix_document_embeddings_embedding_hnsw
     ON document_embeddings USING hnsw (embedding vector_cosine_ops)
     WITH (m = 16, ef_construction = 64);
   CREATE INDEX ix_proposition_embeddings_embedding_hnsw
     ON proposition_embeddings USING hnsw (embedding vector_cosine_ops)
     WITH (m = 16, ef_construction = 64);
   ```
   Downgrade drops both. Use `op.execute` (alembic has no native hnsw support).
2. Add a comment in `db/orm.py` on both embedding columns: "k-NN queries must use
   `<=>` (cosine distance) to hit the HNSW index — `<#>`/`<->` will not."

**Acceptance criteria.**
- [ ] `alembic upgrade head` succeeds on a fresh DB and on a DB at the previous
      head with existing rows.
- [ ] `EXPLAIN SELECT … ORDER BY embedding <=> $1 LIMIT 10` on
      `proposition_embeddings` shows an Index Scan using the hnsw index (with
      `SET enable_seqscan = off` if the table is tiny).
- [ ] Downgrade removes both indexes.

**Tests:** integration (`tests/integration/test_schema_revision_0007.py`, new):
upgrade, insert a handful of normalized vectors, run the EXPLAIN assertion above
(string-match `hnsw` in the plan), downgrade/upgrade idempotence if the existing
migration tests do that elsewhere — mirror `test_schema_revision_0004.py` style.

**Do not:** change the 1024 dimension; add IVFFlat; reorder existing migrations.

---

## R5 — AGE property-id expression indexes on hot vertex labels

**Severity: High (O(n²) ingest writes).**

**Context.** Every graph write resolves vertices by property equality —
`MERGE (s:Span {id: '…'})` (`core/ingest.py:279`), `MATCH (p:Proposition {id: …})`
(`core/proposition.py:433`). Apache AGE stores each vertex label as a table
`<graph>."<Label>"` with an `agtype` `properties` column and does **not** index
properties; each MERGE/MATCH scans the label table. Per-document ingest is
therefore quadratic in span count.

**Changes.**
1. New alembic migration `0008_age_property_indexes` (renumber to the actual next
   slot; depends on R4's revision if both land, else current head). For each hot
   label — `Span`, `Proposition`, `Document`, `Object`, `Actor`, `Fact`, `Box` —
   create a btree expression index on the `id` property. AGE label tables live in
   the schema named after the graph (`iknos` — read the graph name the way
   migration `0001` does rather than hardcoding, if `0001` parameterized it):
   ```sql
   CREATE INDEX ix_age_span_id ON iknos."Span"
     (ag_catalog.agtype_access_operator(properties, '"id"'::ag_catalog.agtype));
   ```
   …one per label, names `ix_age_<label>_id`. Wrap each in a guard so the
   migration succeeds even if a label table does not exist yet (labels are
   created by `0001`/`0004`, so they should all exist — but check `0004` for
   which labels exist before writing the list; only index labels with a created
   table).
   Note: `LOAD 'age';` and `SET search_path` are required in the migration session
   before touching `ag_catalog` types — copy the pattern used by migration `0001`.
2. Verify the index is actually used: AGE's Cypher `MERGE (s:Span {id: 'x'})`
   compiles to a scan with an `agtype_access_operator(...) = '"x"'` filter; the
   expression index matches it. Add an integration test (below) that EXPLAINs an
   equality SELECT through the same expression and asserts index usage.

**Acceptance criteria.**
- [ ] Migration upgrades cleanly on fresh and existing DBs; downgrade drops the
      indexes.
- [ ] `EXPLAIN` on
      `SELECT * FROM iknos."Span" WHERE ag_catalog.agtype_access_operator(properties, '"id"'::ag_catalog.agtype) = '"<some-id>"'::ag_catalog.agtype`
      shows the expression index (seqscan disabled for the test).
- [ ] Existing integration tests (span persistence, proposition layer, domain pack
      load) still pass — the indexes must not change semantics.

**Tests:** `tests/integration/test_schema_revision_0008.py` (new), mirroring the
R4 test style.

**Do not:** add GIN indexes over whole `properties`; index labels not in the hot
list; modify `db/age.py` (writes don't change — only the storage gets indexed).

---

## R6 — Explicit timeouts on LLM and verifier clients

**Severity: Medium (a hung request stalls a whole document's extraction).**

**Context.** `src/iknos/core/llm.py::LLMClient` constructs `AsyncOpenAI` without an
explicit timeout; the MinerU client (`core/mineru.py`) already has
`PARSER_TIMEOUT_S` — mirror that discipline.

**Changes.**
1. `config.py`: add `llm_timeout_s: float = 120.0` (env `LLM_TIMEOUT_S`).
2. `core/llm.py`: pass `timeout=settings.llm_timeout_s` (or a per-instance
   constructor arg defaulting from settings — follow how `base_url`/model are
   currently injected so the verifier client can share the mechanism) into the
   `AsyncOpenAI` constructor. A timeout must surface as the same retryable
   transport-error class the client already retries on (verify tenacity's retry
   predicate covers `APITimeoutError`; if not, add it to the retried exceptions —
   timeouts are transport-class, retryable).
3. `.env.example`: add `LLM_TIMEOUT_S=120`.

**Acceptance criteria.**
- [ ] Client created with explicit timeout; value configurable by env.
- [ ] A timeout is retried like a 5xx (bounded by the existing retry policy), then
      raised.

**Tests:** unit (`tests/unit/test_llm.py` or extend existing): construct the
client, assert the timeout reached the SDK config; simulate `APITimeoutError` via
monkeypatch and assert retry-then-raise. Mirror `tests/unit/test_mineru.py`'s
retry tests.

---

## R7 — `model_version` on the embedding tables

**Severity: Medium (embedding-model migration is otherwise a crisis).**

**Context.** Vectors in `document_embeddings`/`proposition_embeddings` don't record
the producing model; swapping bge-m3 would silently mix incompatible vector
spaces. The segmentation content-hash records the model indirectly, but the rows
themselves must be self-describing for migration/re-embedding.

**Changes.**
1. Migration `0009_embedding_model_version` (renumber to next slot): add
   `model_version TEXT NOT NULL DEFAULT 'BAAI/bge-m3'` to both tables; then drop
   the default (keep NOT NULL) so future writers must supply it explicitly.
2. `db/orm.py`: add the column to `DocumentEmbedding` and `PropositionEmbedding`.
3. Write paths: `core/ingest.py::persist_spans` and the proposition persistence in
   `core/proposition.py` populate it from `EmbeddingSubstrate.model_name` (already
   exposed — see `embeddings.py:60`). Thread the value through existing call
   signatures; do not read global settings inside the write functions.
4. Read paths/k-NN queries (when Phase 2 builds them) must filter
   `model_version = <active model>`; add this as a one-line note in
   `docs/todo_phase_2_graph_construction.md` retrieval items (already added by the
   June 2026 doc update — verify, don't duplicate).

**Acceptance criteria.**
- [ ] Both tables carry NOT NULL `model_version`; existing rows backfilled with
      the bge-m3 default.
- [ ] New ingest writes populate it from the live substrate's `model_name`.

**Tests:** extend `tests/integration/test_span_persistence.py` and
`test_proposition_layer.py`: after ingest, assert `model_version` equals the
substrate's model name on every row.

---

## R8 — `provisional` boolean → `provisional_reasons` set

**Severity: Medium (one flag currently carries three meanings; cheap now, painful after Phase 2).**

**Context.** §3.1/§10 (amended June 2026) replace the single `provisional` boolean
with `provisional_reasons`: a set of enum values, empty ⇔ not provisional. Known
reasons now: `low_faithfulness` (Phase 1), `unresolved_reference` (Phase 2),
`uninferred_budget` (Phase 5). Triage (§11.1) needs the reason to tell the expert
*what judgment is needed*; the quarantine gate (R9) only needs non-emptiness.

**Changes.**
1. `types/epistemic.py`: add
   ```python
   class ProvisionalReason(StrEnum):
       LOW_FAITHFULNESS = "low_faithfulness"
       UNRESOLVED_REFERENCE = "unresolved_reference"
       UNINFERRED_BUDGET = "uninferred_budget"
   ```
   Change `is_provisional(...)` to return `set[ProvisionalReason]` — rename to
   `provisional_reasons_for(faithfulness: float | None) -> set[ProvisionalReason]`
   (returns `{LOW_FAITHFULNESS}` below threshold, else empty; `None` faithfulness →
   empty set, the documented verifier-off mode). Keep a thin
   `is_provisional(...) -> bool | None` wrapper **only if** existing callers need
   it; prefer migrating the callers.
2. `types/nodes.py::Proposition`: replace `provisional: bool | None` with
   `provisional_reasons: list[str]` (Pydantic; default `[]`). (List, not set, for
   stable JSON/AGE serialization; treat as a set semantically — deduplicate on
   write.)
3. `core/proposition.py`: persist `provisional_reasons` on the node (AGE is
   schemaless — no migration needed) **and keep writing the legacy boolean
   `provisional`** (`true` iff reasons non-empty) for one transition release, with
   a `# TODO(remove after Phase 2 lands)` marker. Update the extract `Action`
   outputs to include the reasons.
4. Grep for every reader of `provisional` (`grep -rn "provisional" src/ tests/`)
   and migrate each to the reasons set (or to non-emptiness where boolean intent).

**Acceptance criteria.**
- [ ] A low-faithfulness proposition persists
      `provisional_reasons=["low_faithfulness"]` and `provisional=true`.
- [ ] A high-faithfulness proposition persists `provisional_reasons=[]` and
      `provisional=false`.
- [ ] Verifier-off mode: reasons `[]`, legacy field `null` (unchanged semantics).
- [ ] No remaining production reads of the boolean except the legacy write.

**Tests:** update `tests/unit/test_epistemic.py` (reason derivation incl.
threshold edge), `tests/integration/test_proposition_layer.py` (persisted fields).

---

## R9 — Quarantine gate function (G1.6 enforcement seam)

**Severity: Medium (the gate exists on paper; nothing enforces it).**

**Context.** `gap_phase_1_ingest.md` G1.6: provisional propositions must not drive
high-stakes moves (a `REFUTES`). Evidential-edge creation is Phase 2, but the gate
function must exist, be tested, and be impossible to forget — Phase 2's edge writer
calls it. Depends on R8.

**Changes.**
1. New module `src/iknos/core/quarantine.py`:
   ```python
   class QuarantinedPropositionError(Exception): ...

   class Stakes(StrEnum):
       LOW = "low"      # e.g. a corroborating SUPPORTS among many
       HIGH = "high"    # e.g. any REFUTES; a sole-support SUPPORTS

   def assert_not_quarantined(
       proposition_reasons: Collection[str],
       stakes: Stakes,
   ) -> None:
       """Raise QuarantinedPropositionError when a provisional proposition would
       drive a high-stakes move (§3.1: quarantined from high-stakes use). LOW
       stakes pass regardless; HIGH stakes require an empty reasons set."""
   ```
   Implement exactly that rule (HIGH + non-empty reasons → raise, message lists
   the reasons). Keep it pure — no DB, no settings.
2. Document the call contract in the module docstring: *every* code path that
   creates a `REFUTES` edge, or a `SUPPORTS` edge that is the target's sole
   support, must call this with `Stakes.HIGH` before writing. (Phase 2 wires it;
   `todo_phase_2_graph_construction.md` references this module by name.)

**Acceptance criteria / tests** (`tests/unit/test_quarantine.py`, new):
- [ ] HIGH + `{low_faithfulness}` → raises, message contains the reason.
- [ ] HIGH + empty → passes. LOW + any reasons → passes.
- [ ] Pure function: importable without `DATABASE_URL` set.

---

## R10 — Serve embedding inference out-of-process

**Severity: Medium (in-process CPU torch inference inside the API/ingest process is the throughput floor).**

**Context.** `EmbeddingSubstrate` loads bge-m3 into the calling process. §6
(amended) requires embedding inference behind the same swappable-service seam as
the LLM and the parser. Pattern to copy: `core/mineru.py` (httpx client, pydantic
wire validation, retry transport/5xx only) and `core/parse.py` (protocol +
factory + null/local fallback).

**Changes.**
1. Define `EmbeddingBackend` protocol in `core/embeddings.py` with the two
   existing methods (`embed_document(text) -> DocumentContext`,
   `embed_passages(texts) -> list[list[float]]`). The current in-process class
   already satisfies it — keep it as the default/local backend (used by tests).
2. New `core/embeddings_http.py::HTTPEmbeddingBackend` targeting a
   **Text-Embeddings-Inference (TEI)**-compatible server *for `embed_passages`*,
   and a custom endpoint for `embed_document` (TEI does not return token
   embeddings + offsets; the windowed-late-chunking document path needs them).
   Wire schema (ours, versioned, mirroring the MinerU pattern): POST
   `/embed_document` `{text, window_tokens, overlap_tokens}` →
   `{model_version, offsets: [[s,e],…], embeddings: [[…],…]}` (fp32 lists);
   POST `/embed_passages` `{texts}` → `{model_version, embeddings}`. Validate with
   pydantic; reject mismatched lengths. Retries: transport/5xx only; timeout from
   new `EMBEDDINGS_TIMEOUT_S` (default 300).
3. `make_embedding_backend()` factory in `core/embeddings.py` keyed on new
   settings `EMBEDDINGS_BASE_URL` (empty ⇒ in-process local backend — exactly the
   `parser_base_url` pattern in `config.py`).
4. The server itself is **out of scope** (ops-side, like the MinerU service); this
   task ships the client + seam + local fallback. Record the server requirement in
   `local-llm-setup/` as a stub README section.

**Acceptance criteria.**
- [ ] `EMBEDDINGS_BASE_URL` unset → behaviour byte-identical to today (local
      backend; all existing tests pass untouched).
- [ ] HTTP backend: wire validation rejects length-mismatched payloads loudly;
      5xx retried, 4xx not (mirror `test_mineru.py` test structure).
- [ ] `DocumentContext` produced by the HTTP backend is interchangeable with the
      local one (same pooling results on a fixture given identical vectors).

**Tests:** `tests/unit/test_embeddings_http.py` (new), using httpx MockTransport
exactly as `test_mineru.py` does.

**Do not:** remove or deprecate the in-process backend; build the server; change
`DocumentContext`.

---

## R11 — Background job queue for ingest (procrastinate)

**Severity: Medium (real-corpus ingest is currently a single-process foreground job with no retry/backpressure).**

**Context.** §6 (amended) pulls job orchestration forward from Phase 6 to Phase 2
and selects **procrastinate** (Postgres-native task queue: LISTEN/NOTIFY over the
existing engine — no new infra, consistent with the single-engine principle and
self-hosted/open-source principle 7). The queue also realizes the §6 concurrency
contract: **one ingest worker per document; graph writes for one investigation
serialize through one queue**.

**Changes.**
1. Add `procrastinate[psycopg]` (check the current extra name for async SQLAlchemy
   coexistence; procrastinate manages its own connection) to `pyproject.toml`.
2. New module `src/iknos/jobs/__init__.py` + `src/iknos/jobs/app.py`: a
   procrastinate app bound to `DATABASE_URL`; one task
   `ingest_document_bytes_job(document_id, content_b64 | storage_ref, title, box)`
   wrapping `core/ingest.ingest_document_bytes` in a session/transaction (commit
   on success — the caller-owned-transaction contract is satisfied by the job
   being the caller), with retry policy: max 3 attempts, exponential backoff,
   retry only on transport-class errors (`ParserUnavailable`-style/5xx/DB
   disconnect), never on validation errors (`DocumentTooLongError`,
   `ParseResult` validation, `DocumentResegmentationError` — these are terminal).
3. Queue layout: queue name `ingest:<box_id>` with **per-queue concurrency 1**
   (procrastinate worker `--concurrency` + queue locks — use procrastinate's
   `queueing_lock`/`lock` so two jobs for the same `document_id` cannot run
   concurrently; lock key = document id).
4. Migration: procrastinate needs its schema — add an alembic migration that runs
   `procrastinate schema --apply`-equivalent SQL (procrastinate exposes the DDL;
   embed it via its API in the migration, or document the one-shot
   `uv run procrastinate schema --apply` step in `MIGRATIONS.md` if embedding is
   brittle — choose one and write it down in the PR).
5. `api/main.py`: add `POST /documents` (multipart upload) that enqueues the job
   and returns the job id + document id; add `GET /jobs/{id}` returning
   procrastinate job status. Keep both behind the existing stub structure — no
   auth yet (tracked in Phase 6/7 docs).
6. `compose.yaml`: add a `worker` service (same image as `api`, command
   `uv run procrastinate worker …`). **Do not start it; compose changes are
   reviewed, not run** (host policy: no `docker compose up` without approval).

**Acceptance criteria.**
- [ ] A document enqueued via the API is ingested by a worker process with
      committed spans + Action rows; job status reaches `succeeded`.
- [ ] A failing parse (validation error) goes to `failed` without retry; a
      simulated transport error retries up to 3 times.
- [ ] Two jobs for the same document id cannot run concurrently (lock test).
- [ ] No behaviour change for direct/synchronous `ingest_document_bytes` callers
      (tests untouched).

**Tests:** unit-test the retry-classification helper (pure); integration test the
enqueue→run path using procrastinate's testing utilities (in-memory/sync worker
mode — `procrastinate.testing.InMemoryConnector`) so no live worker container is
needed.

---

## R12 — Observability floor: metrics on `Action` records

**Severity: Medium (cost discipline §6.1 and Trials A/C need these numbers; cheapest now).**

**Context.** The `actions` table records what happened but not duration, token
spend, or cache behaviour. Add a `metrics` JSONB column and populate it from the
three instrumented paths that exist today.

**Changes.**
1. Migration `00XX_actions_metrics` (next slot): `ALTER TABLE actions ADD COLUMN
   metrics JSONB NOT NULL DEFAULT '{}'::jsonb`.
2. `db/orm.py::Action` + `provenance/action_log.py::record_action`: accept an
   optional `metrics: dict` (default empty).
3. Populate:
   - `core/llm.py`: return usage from the completion (`response.usage` —
     prompt/completion tokens) alongside the parsed result (smallest viable
     change: a small `LLMResult` dataclass or a second return value — pick what
     disrupts callers least and update them);
   - extract/verify Actions in `core/proposition.py`: `{duration_ms,
     prompt_tokens, completion_tokens, n_samples, cache_hit: false}`; the
     idempotency-skip path writes no Action today — leave that as-is, but when a
     `StaleExtractionError` re-extract happens, `cache_hit: false` and a
     `stale_rerun: true` flag;
   - parse/segment Actions in `core/ingest.py`: `{duration_ms, n_spans,
     n_skipped_whitespace}`.
   Use `time.monotonic()` deltas; no new dependencies.

**Acceptance criteria.**
- [ ] Every new Action row carries `metrics` with at least `duration_ms`.
- [ ] LLM-backed actions carry token counts when the backend reports usage; absent
      usage → keys omitted, not zeroed (don't fabricate).

**Tests:** extend the existing integration tests that already assert Action rows
(`test_ingest_layout.py`, `test_proposition_layer.py`): assert `metrics` presence
and `duration_ms > 0`.

---

## R13 — Phase 2 prerequisite design documents (three spikes)

**Severity: High (Phase 2 cannot start without them; each is a decision, not code).**

**Context.** Phase 2 references three subsystems no document specifies. Each
sub-task below produces a **design doc** in `docs/`, not code. An agent executing
these must read `architecture.md` §5.2/§9.1/§14 and
`todo_phase_2_graph_construction.md` first, survey the named options, and write a
decision document in the style of the existing docs (decisive, referenced,
with explicit acceptance metrics tied to the named Trials).

- **R13a — `docs/design_entity_linking.md`.** Decide the anchor-first entity
  linker: candidate generation from the domain-pack taxonomy (label/alias exact +
  fuzzy match, embedding similarity for blocking only), scoring on
  relational/contextual evidence per §5.2, thresholds for
  anchored/candidate/unresolved. Must specify: the data contract (input
  `Mention` + context spans; output scored `REFERS_TO`/anchor candidates), how
  cross-domain ambiguity uses active-pack scope, and the Trial A4/A6 metrics that
  gate automation (κ > 0.6 etc. — cite `todo_trials.md`). Survey: GENRE-style
  generative linking, bi-encoder + cross-encoder rerank, plain
  gazetteer+heuristics — recommend one for the MVP with rationale (expect
  gazetteer+embedding-blocking+LLM-adjudication to win at MVP scale; justify or
  refute).
- **R13b — `docs/design_meronymy_induction.md`.** Decide the induced-`directPartOf`
  fallback: candidate patterns (compositional NPs, "Y of X", possessives,
  "consists of"), the extraction mechanism (rule-based over the existing
  propositions vs a dedicated LLM pass — recommend, with cost), confidence
  assignment, expected expert-triage load per document (estimate from a sample),
  and the A4 redesign trigger (<50% anchoring coverage). Must restate the
  meronymy-type taxonomy (Winston/Chaffin/Herrmann's six; this repo's
  `domain/pack.py::MeronymyType` already enumerates them — the doc maps each to
  roll-up-safe yes/no) and the rule that only component-integral chains roll up.
- **R13c — `docs/design_credibility_derivation.md`.** Operationalize §9.1
  conditional credibility: `effective = Box.reliability_prior ×
  f(interest_alignment, epistemic_class)`. Must decide: where
  `interest_alignment` comes from (domain-pack role patterns + per-claim LLM flag,
  per §9.1), where the computed value lives (computed at use-time, optionally
  materialized — §10 already says this; the doc specifies the function `f`, its
  table of multipliers, and the recompute triggers), and who consumes it (edge
  `significance` seeding in Phase 4; judgement-class proposition weighting).
  Include 3 worked examples (supplier blames transport; admission against
  interest; neutral observation).

**Acceptance criteria (each doc):** decisive (one recommendation, alternatives
dismissed with reasons); references architecture sections by §; names the gating
trial and its threshold; ends with an implementation task list small enough that
each item is < 1 week. Link each doc from `todo_phase_2_graph_construction.md`'s
entry-gates section (already references these filenames).

---

## Explicitly out of scope for this file

- **E1-lite definition** — added to `todo.md` / `todo_trials.md` (process, not code).
- **Auth design** — tracked as a Phase 6/7 prerequisite item in those phase docs.
- **Supersession semantics, concurrency model, sensitivity-propagation timing,
  hybrid fusion** — decided and written in `architecture.md` (§7.4, §6, §9.1, §4);
  implementation lands with their owning phases (3, 2/3, 3, 2 respectively).
