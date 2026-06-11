# Architecture & Plan Review — 2026-06-10

Critical review of `architecture.md`, the phase plans (`todo*.md`, `gap_*.md`,
`todo_trials.md`), and the implemented code (`src/iknos/`, migrations, the in-flight
`feat/g1.0b-mineru-client` branch). Focus: significant technical gaps, mistakes in the
plan, and fault-leading problems — not style. Findings reference architecture sections
(§) and `file:line`.

**Overall judgment.** The architecture is unusually well-reasoned: the two-layer
propagation split (§12), the propose/dispose invariant, the gate discipline
(A0–A7 → E1 go/no-go), and the fail-loud trust boundaries in the shipped code are all
genuinely strong. The significant problems are concentrated in four places: (1) the
embedding substrate silently breaks on real-sized documents, (2) the shipped
multi-sample consistency machinery is blind to the one error class §3.1 exists to
catch, (3) the storage layer has no graph indexes and several unbudgeted scale cliffs,
and (4) the plan's critical-path assets (evaluation corpus, baselines, AGE benchmark)
are sequenced too late relative to what depends on them.

---

## Severity index

| # | Finding | Severity | Where |
|---|---------|----------|-------|
| C1 | 8,192-token truncation silently drops document content from the dense index | **Critical** | `embeddings.py:71` |
| C2 | Agreement clustering is polarity-blind — inflates confidence on negation flips | **Critical** | `consistency.py:67` |
| C3 | No indexes on AGE property lookups; every MERGE/MATCH is a label-table seq scan | **Critical** | migrations 0001–0006 |
| A1 | Table ingestion (§1 rule a) has no representation in the parse contract | High | `parse.py`, §1 |
| A2 | "Cache the contextualized token embeddings" is not implemented; G1.10 will re-pay | High | §1, `embeddings.py` |
| A3 | Sparse index is not BM25; ranking assumption in §4 doesn't hold on Postgres FTS | High | migration 0003, §4 |
| A4 | Cache invalidation depends on a manually-bumped `EXTRACT_SCHEMA_VERSION` | High | `cache.py`, `proposition.py:131` |
| A5 | Dense rows carry no embedding-model identity; model swap is undetectable | High | `orm.py`, migrations 0002/0003 |
| A6 | Viterbi/multiplicative confidence has a structural depth penalty | High | §12, `epistemic.py` |
| P1 | A0 evaluation corpus is the critical-path asset and is unscoped | **High (plan)** | `todo_trials.md` |
| P2 | C3 (AGE viability) is sequenced after the phases that build on AGE | **High (plan)** | `todo_trials.md` |
| P3 | A5 thresholds shipped to production before the metric that justifies them exists | High (plan) | G1.3/G1.5 |
| P4 | Multi-sample with temperature 0 yields a degenerate, always-1.0 agreement | Medium | `config.py`, `proposition.py` |
| P5 | E1 baseline implementations unscoped; no auth/deployment phase anywhere | Medium (plan) | `todo.md` |
| R1–R8 | Code-level robustness items (error isolation, verifier None, injection fuzzing…) | Medium/Low | various |

---

## 1. Critical findings

### C1. The embedding substrate silently truncates real documents (`embeddings.py:71`)

`embed_document()` tokenizes with `truncation=True, max_length=8192` and returns no
signal that truncation occurred. Everything downstream assumes the whole document is
covered:

- `pool_span()` finds no overlapping tokens for any span past the truncation point and
  returns a **zero vector** (`embeddings.py:42`); `persist_spans` then skips the dense
  row. Result: every span beyond ~8k tokens (~25–35 KB of text; roughly 12–20 PDF
  pages) is **invisible to dense retrieval and to the §5.1 candidate funnel**.
- Segmentation (`segmentation.py`) computes adjacent-window similarities over the same
  truncated context, so boundary placement past the cutoff is undefined as well.

This is precisely the "silent false negative — the dangerous kind" that §5.1 warns
about, manufactured by the ingest layer itself. The stated target inputs are real case
PDFs ("tens of dense documents", §6.1); most will exceed 8k tokens, so in practice the
pipeline currently indexes the *front* of every document and drops the rest without an
error, a flag, or an Action record.

The architecture itself has a gap here: §1 says "run a long-context embedding model
over the whole document once" and never addresses documents longer than the model
context. The late-chunking literature this is borrowed from handles this with
**overlapping macro-windows** (embed overlapping 8k windows, pool each span from the
window where it sits furthest from the edges).

**Recommendation (ordered):**
1. *Immediately* (one-line guard): detect truncation (`len(tokens) == max_length`) and
   raise, exactly like `DocumentResegmentationError` — fail loud until long docs are
   handled. A silent wrong result is worse than a refusal to ingest.
2. *G1.x (before MinerU client ships real PDFs)*: windowed embedding with overlap and
   per-span window selection. This changes `DocumentContext` only; span IDs, hashes,
   and persistence are unaffected.
3. Record truncation/window layout in the segment Action so provenance shows which
   context produced each vector.

### C2. Agreement clustering cannot see polarity — it rewards the worst instability (`consistency.py:67-92`)

`cluster_candidates()` assigns candidates to clusters **purely by embedding cosine ≥
0.86**. The `Candidate` dataclass carries `polarity`, `modality`, and
`epistemic_class`, but they play no role in cluster identity. Sentence-embedding
models (bge-m3 included) map negation pairs very close together — "the bearing
failed" vs "the bearing did not fail" typically scores well above 0.9 cosine, over the
0.86 threshold.

Consequences, both directly contrary to §3.1:

- **Agreement is inflated exactly when the extractor is unstable on polarity.** If 3
  of 5 samples assert and 2 negate, they form one cluster with agreement 5/5 = 1.0 —
  maximum confidence assigned to the single most dangerous instability the
  architecture identifies ("a dropped negation … is a confidently-wrong atom").
- **The persisted polarity is a coin flip.** `canonical_of()` picks the medoid by
  embedding centrality; in a mixed-polarity cluster the medoid's polarity depends on
  the sample distribution, and the losing polarity leaves no trace.

The same applies (more weakly) to modality: "the bearing failed" vs "the bearing
possibly failed" will co-cluster, flattening hedges — the other §3.1 failure mode.

**Recommendation:** make the structured fields part of cluster identity. Minimal fix:
hard-partition candidates by `polarity` (and arguably `epistemic_class`) before cosine
clustering. Better: when near-identical embeddings split across polarities, treat that
as a **negative** consistency signal — drive agreement *down* and flag the span
provisional, because the extractor is telling you it cannot read the sentence's
direction. This is a small change in pure, well-tested code; do it before Trial A5
fits the threshold, or the fitted threshold will bake the bug in.

### C3. The graph has no property indexes — MERGE-by-id is a sequential scan

Migrations create relational indexes (actions, embeddings, lexical) but **no indexes
on any AGE label table**. AGE stores properties in an `agtype` column; without
explicit expression/GIN indexes, every `MERGE (n {id: ...})`, every box-scoped
`MATCH`, and every bitemporal as-of filter is a sequential scan of the label's heap
table.

Phase 1 volumes hide this. It becomes a cliff at exactly the points the plan leans on
AGE hardest:

- **Phase 2 entity resolution** runs *continuous* candidate lookups and `SAME_AS`
  component queries — per-mention, per-fact.
- **Idempotent re-ingest** does a MERGE per span; cost becomes O(spans²) per document
  corpus re-run.
- **Bitemporal queries** (`valid_from`/`valid_to` in agtype properties) are
  unindexable without planned expression indexes — §13 already flags AGE density risk,
  but the mitigation is concrete and cheap and belongs in a migration now.

**Recommendation:** add a migration creating, per vertex label, a btree expression
index on `agtype_access_operator(properties, '"id"')` (and `"box"`), plus GIN on
`properties` for the labels that get ad-hoc filters. Fold "do indexed plans actually
get used through the cypher() call path?" into an early C3 benchmark (see P2) —
`EXPLAIN` through AGE has sharp edges, and discovering them in Phase 3 is expensive.

---

## 2. Architecture gaps and mistakes

### A1. Tables: the contract cannot carry what §1 promises

§1 integration rule (a): "tables ingest as structured observations (rows/cells →
propositions with column semantics), not flattened prose." But `ParseResult` is a
single reading-order text blob plus linear `[start, end)` element ranges
(`parse.py`), and the MinerU wire schema mirrors that. A `ParseKind.TABLE` element is
just a char range — the 2-D structure (rows, headers, cell adjacency) is destroyed at
the trust boundary and cannot be reconstructed downstream. Nothing in the G-task list
covers structured table payloads; Phase 2's "rows/cells → propositions" will have
nothing to read.

This is the right time to fix it: the wire contract is being defined on this branch.
Add an optional structured payload on table elements (normalized cell grid, each cell
with text + offset range + bbox so provenance still resolves to spans), even if the
consumer is deferred. Retrofitting a wire contract after a MinerU service adapter
ships is strictly more work. Figures are fine — "located now, interpreted later" is
representable; tables are not.

### A2. "Embed once" is currently "embed once per process"

§1: "Contextualize the whole document a single time; cache the contextualized token
embeddings… derive every granularity from the cached result." `DocumentContext` is an
in-memory tensor, discarded after ingest. That's fine for the current single-level
slice, but G1.10 (multi-level spans, RAPTOR) and any later re-pooling (e.g., §5.1
coarse-to-fine over new levels) will re-run the model over every document — the exact
L× cost §1 says this design avoids.

Decide explicitly: either persist token embeddings (≈ 16–32 MB/doc at fp16/fp32 —
acceptable at "tens of documents", a table keyed by `(document_id,
embedding_model)`), or amend §1 to "recompute per run" and make G1.10's cost model
honest. The current state is a quiet contradiction between the design's core economy
claim and the implementation.

### A3. The sparse index is not BM25 and won't behave like it

§4 specifies "TF-IDF / BM25". The implementation is Postgres FTS: a GIN index over a
`'simple'`-config tsvector (migration 0003). Postgres `ts_rank` is neither TF-IDF nor
BM25 — no IDF term, no document-length normalization. For the stated purpose (exact
tokens: names, codes, acronyms) *recall* is fine, but hybrid-retrieval *ranking* and
any score fusion tuned on BM25 assumptions will not transfer.

Options: (a) accept it, rank-fuse with RRF (rank-based fusion is insensitive to score
semantics) and correct §4; (b) adopt a real BM25 extension — note ParadeDB
`pg_search` and VectorChord-BM25 are AGPL, which collides with the project's license
boundary unless service-isolated like MinerU. (a) is almost certainly right at this
scale; the point is to decide it rather than inherit a false assumption into the §5.1
funnel tuning (Trial A1).

### A4. Prompt changes can silently serve stale extractions

`extraction_content_hash` (`cache.py`) keys on `schema_version` — a hand-bumped
constant (`EXTRACT_SCHEMA_VERSION = 1`, `proposition.py:131`) whose docstring says
"bumped on any prompt / schema / enum change." That is a human-discipline guard on
exactly the failure G1.7 exists to prevent: edit the prompt, forget the bump, and
every cached span replays the old extraction with no signal. The fix is mechanical —
hash the rendered system prompt template and the JSON schema into the key (they're
both in hand at call time) and keep `schema_version` only for semantic versioning of
the *output* shape. Cheap, removes a whole failure class.

### A5. Embedding vectors carry no model identity

`document_embeddings` / `proposition_embeddings` rows have no `model` column. The
substrate is self-describing (`embeddings.py:60`) and the model feeds the *segmentation*
hash, but the stored vectors themselves are unlabeled. Swap or upgrade the embedding
model and you get a mixed-space index — cosine comparisons across spaces are
meaningless, and nothing can even detect the condition. Add `model` (+ dimension) to
both tables and make ingest refuse to upsert into a table populated under a different
model, mirroring the resegmentation guard. Also define the reindex path (it's just
"re-run pooling from raw text", but it should exist as a script before it's needed).

### A6. Multiplicative confidence stacks have a built-in depth bias

The confidence pipeline multiplies at every stage: `faithfulness = verify ×
agreement` (`combine_faithfulness`), credibility multiplies on judgements (§9.1), and
Layer B's Viterbi semiring multiplies along every derivation chain (§12). Under
`max-·`, a conclusion five `DERIVED_FROM` hops from 0.9-confidence facts bottoms out
near 0.59 *regardless of how good the evidence is* — confidence becomes substantially
a measure of derivation depth, not epistemic quality. Deep, careful derivations get
structurally punished relative to shallow ones; QBAF base scores inherit the bias;
the §11.2 acceptability bands then cut at thresholds whose meaning varies with chain
length.

§12 already lists Gödel `max-min` as the alternative — under `min`, a chain is as
strong as its weakest link and depth alone costs nothing, which matches the intuition
the architecture elsewhere endorses (ordinal confidence, ordering-driven QBAF). This
should be **decided at Phase 3 entry with a fixture demonstrating both behaviors**,
not defaulted to Viterbi. If Viterbi is kept, the banding thresholds must be
depth-aware, which is strictly more complicated. (The same compounding shows up at
the perception layer: verify × agreement × credibility means three noisy [0,1]
estimates multiply — consider whether `min` is also the right combiner there once
Trial A5's metric exists.)

---

## 3. Plan and sequencing issues

### P1. A0 — the evaluation corpus — is the critical path and has no owner, scope, or start date

Every gate in the plan (A1–A7, B1/B2, E1) consumes the A0 planted corpus and harness.
Building it is real work: authoring documents with planted contradictions and
dissimilar refuters, gold SUPPORTS/REFUTES edges, gold entity clusters, gold
faithfulness labels, ≥2 annotators with κ — weeks, not days. It depends on **nothing
unbuilt** (it's documents and labels), yet it is sequenced as if it appears when
Phase 4 needs it. Start it now, in parallel with the Phase 1 tail. If it slips, every
gate slips, and the project's whole "earn the complexity" discipline becomes
unenforceable in practice.

### P2. C3 (AGE viability) is scheduled after the phases that bet on AGE

C3 sits in the trials list as a scale benchmark, but §13 itself names AGE-under-this-
schema-density "a real viability risk." Phases 2–5 build entity resolution, recursive
retraction, and bitemporal supersession directly on AGE; if C3 then fails, the rework
scales with everything built in between. The benchmark is cheap to pull forward: a
synthetic graph at target density (provenance + two annotations + sensitivity +
bitemporal fields on every element), the four real query patterns (box-scoped match,
`WITH RECURSIVE` reachability, SCC extraction, as-of reads), measured before Phase 2
starts. Days of work; de-risks the single biggest potential architecture swap. Pair
it with the C3 index work (finding C3 above).

### P3. The consistency/faithfulness machinery shipped before its measuring instrument

G1.3/G1.4/G1.5 are in production code with live constants — agreement threshold 0.86,
the multiplicative combiner, quarantine cutoffs — while the A5 metric that would
validate them "remains to be wired" (`gap_phase_1_ingest.md`). Building mechanism
before metric is backwards relative to the project's own philosophy, and finding C2
shows the cost: an instrument would likely have caught polarity-blind clustering
immediately. Wire the A5 metric (entailment accuracy, negation/modality preservation,
binding accuracy) against even a *small* labeled set before any further
perception-layer tuning; treat 0.86 as unvalidated until then.

### P4. Default config makes multi-sample extraction silently degenerate

`LLM_EXTRACT_SAMPLES` defaults to 1 and sampling defaults to temperature 0. If an
operator sets `n_samples > 1` and leaves temperature at 0, all N samples are (near-)
identical, every cluster gets agreement 1.0, and the consistency signal reads "maximum
confidence" while measuring nothing. Add a startup guard: `n_samples > 1` requires
`temperature > 0` (or warn loudly). One line in config validation; prevents a
quietly-meaningless confidence pipeline in a misconfigured deployment.

### P5. Unscoped prerequisites and missing operational phases

- **E1 baselines.** "Material lift over plain RAG / agentic RAG / expert+search" is
  the go/no-go, but no task builds those baselines, and a weak baseline invalidates
  the comparison in the system's favor. Scope them; they're also reusable as the
  retrieval sanity check for Phase 1.
- **No auth/deployment/observability anywhere in Phases 0–7.** The §9.1 sensitivity
  lattice and clearance-relative projection presuppose authentication and authorization
  infrastructure that no phase builds; `api/main.py` is a bare health endpoint. Equally
  absent: containerized service packaging (including the MinerU AGPL service-edge
  enforcement in build tooling), Postgres backup/restore for what is intended to be the
  durable record of investigations, and monitoring. These belong as explicit Phase 6/7
  scope items now, not discoveries during them.
- **Quarantine gating (G1.6)** is deferred to Phase 2 — acceptable, but make it a
  Phase 2 *entry* criterion; until then `provisional` flags are decorative and nothing
  enforces §3.1's "quarantined from high-stakes use."

---

## 4. Implementation robustness (code-level)

Smaller fault-leading items, roughly ordered:

- **R1 — No per-span error isolation** (`proposition.py:578`): a bare
  `asyncio.gather` means one transient LLM failure on one span aborts the whole
  document run; with N samples × S spans the failure probability compounds. Use
  `return_exceptions=True` (or per-span try) + record failed spans and continue —
  the content-addressed idempotency already makes re-runs cheap, so lean on it.
- **R2 — Verifier output not defended** (`verify.py` → `proposition.py:349`): an
  unparseable/None verdict reaches `faithfulness_from_verdict` and crashes the span.
  Treat verifier failure as "verdict unavailable → provisional", not an exception.
- **R3 — Cypher injection surface is hand-rolled at an adversarial boundary**
  (`db/age.py`): document text and LLM output are string-interpolated into openCypher
  via `cypher_map` escaping. The gap review dismissed this for current paths, but the
  *input is untrusted documents* and the escaping is bespoke. Minimum: property-based
  fuzz tests round-tripping hostile strings (quotes, backslashes, unicode escapes,
  agtype syntax) through `cypher_map` → AGE → read-back; prefer AGE's parameter
  support where the call path allows.
- **R4 — Zero-vector sentinel coupling** (`embeddings.py:42` ↔ ingest skip logic):
  the whitespace-span contract is "returns `[0.0]*1024` and the caller knows to
  skip." Return `None` or raise instead; a future caller that forgets the convention
  poisons the ANN index silently. (Interacts with C1 — truncated spans take this same
  path today.)
- **R5 — Idempotency lookups unindexed for parser/segmenter actors**: migration 0006
  covers only `actor='propositionizer'`; parse/segment hash lookups scan
  `actions` ordered by timestamp. Same pattern, two more partial indexes. Also: the
  append-only `actions` table has no retention/partitioning plan and is now on the
  hot path of every ingest decision — fine for years at this scale, but state that.
- **R6 — Semaphore permits held across unbounded retries**: tenacity allows ~5
  attempts × up to 30 s backoff while holding a concurrency permit; a hung vLLM
  endpoint stalls the whole budget. Add an overall per-call deadline.
- **R7 — Event-loop blocking on embeddings**: `asyncio.to_thread(embed_passages…)`
  helps, but a large batch on CPU still serializes everything behind torch. Fine for
  now; becomes the throughput ceiling when ingest goes multi-document — worth a note
  in G1.8's design.
- **R8 — `EmbeddingSubstrate` lifecycle**: model loaded in `__init__`, no
  release path; long-running workers re-instantiating it leak VRAM. Context-manager
  or module-level singleton.

---

## 5. What is solid (keep doing this)

For balance, the things this review deliberately does **not** want changed:

- **The two-layer propagation argument (§12)** — the algebraic forcing argument
  (inverse vs idempotence) is correct and the cleanest statement of why naive
  "confidence-weighted truth maintenance" designs fail. Subject to A6's semiring
  choice, this core is right.
- **Gate discipline** — E1 as an explicit early go/no-go with named differentiator
  axes, the validity ladder, refuter-recall as a first-class measured risk: this is
  rare rigor. The issues above are about *sequencing* the gates' prerequisites, not
  their design.
- **Trust-boundary discipline in code** — dual validation gates on the MinerU
  contract, never-retry-4xx, immutability guards (`DocumentResegmentationError`,
  `PackImmutabilityError`), deterministic uuid5 IDs + MERGE idempotency,
  caller-owns-transaction. The shipped slices are well-built.
- **License boundary handling** — MinerU as AGPL-at-the-service-edge is the right
  call; extend the same screening to any BM25 extension (A3).
- **Pure-module separation** (`cache`, `consistency`, `epistemic`, `parse`) — this is
  what made this review's code verification fast; it will do the same for the trials.

---

## 6. Recommended order of attack

1. **Truncation guard** (C1 step 1) — one line, stops silent data loss today.
2. **Polarity-aware clustering** (C2) — small pure-code change; do before A5 fits
   thresholds.
3. **AGE property-index migration + pulled-forward C3 density benchmark** (C3, P2) —
   before Phase 2 design hardens.
4. **Start A0 corpus now** (P1) — parallel track, no code dependencies.
5. **Wire the A5 metric** (P3) and re-fit the 0.86 threshold and quarantine cutoff.
6. **Windowed long-document embedding** (C1 step 2) — before the MinerU client starts
   feeding real PDFs.
7. **Contract slot for structured tables** (A1) — while the wire schema is still on a
   branch.
8. **Hash the prompt into the cache key; add model column to embedding tables**
   (A4, A5) — two small migrations/edits closing silent-staleness classes.
9. **Decide Viterbi vs Gödel with a depth-bias fixture at Phase 3 entry** (A6).
10. Robustness batch R1–R6 as a single hardening PR.

Items 1, 2, 4, 8 are cheap and close whole failure classes; none conflict with the
in-flight MinerU branch.
