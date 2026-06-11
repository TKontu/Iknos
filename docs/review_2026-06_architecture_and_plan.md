# Architecture & Plan Review — June 2026

> **Status: findings record; remediation tracked in `gap_review_2026-06.md`
> (R1–R13).** A parallel review pass was merged independently as **PR #30**
> (`review_2026-06_architecture_plan.md` + G1.13–G1.19 / G0.R2 plan edits) and
> overlaps several findings here (long-document truncation, AGE property indexes,
> embedding-model identity, RRF fusion, C3 timing) — for those, #30's plan items
> govern. The findings **unique to this review** are: the Cypher `$$`
> dollar-quote injection (C2), missing HNSW indexes on the pgvector tables (C3),
> the absent concurrency model (G2), supersession semantics (G6),
> `provisional_reasons` (G7), the E1-lite/E0 early kill-switch (G3), job
> orchestration + out-of-process embedding serving (P1), and the Phase 2
> prerequisite design docs (G5). See the reconciliation header in
> `gap_review_2026-06.md`.

Independent review of `architecture.md`, the phase plans (`todo_*.md`, `gap_*.md`),
and the implemented code (Phases 0–1) against the stated target: a **highly
performing and usable** system. Findings are ordered by severity. Each cites the
file and, where relevant, the line that carries the problem.

**Verdict in one paragraph.** The architecture is unusually rigorous and the
Phase 0–1 code is disciplined (idempotent ingest, content-addressed caching,
guard-railed parsing, real tests). The serious problems are concentrated in three
places: (1) a small number of **fault-leading implementation defects** that will
silently lose or corrupt data on real inputs — most importantly the
8,192-token embedding truncation that silently drops the tail of every long
document; (2) **performance assumptions written into the plan but not into the
schema or code** — no ANN index, no AGE property indexes, in-process embedding
inference, no job orchestration before Phase 6; and (3) **plan-sequencing
mistakes** — the go/no-go gate sits after the most expensive build phases,
interface-shaping decisions are classified as non-blocking trials, and Phase 2's
prerequisites (entity linking, meronymy induction, credibility derivation) are
referenced everywhere and specified nowhere.

---

## 1. Critical — fault-leading defects in current code

### C1. Silent data loss for documents longer than 8,192 tokens

`src/iknos/core/embeddings.py:71` — `embed_document()` tokenizes with
`truncation=True, max_length=8192`. For any document longer than ~8k tokens
(≈ 15–25 pages; i.e. **most real case PDFs**, the system's stated primary input):

- `DocumentContext.offset_mapping` ends at the truncation point, so
  `pool_span()` finds no overlapping tokens for tail spans and returns a **zero
  vector** (`embeddings.py:40-42`).
- Segmentation runs over the same truncated context, so boundary detection in
  the tail is operating on zeros/garbage.
- `persist_spans()` then **silently excludes** zero-vector spans from
  persistence (`core/ingest.py:95-103`, `_is_zero_vector` at `:162`) — they are
  counted in `PersistResult.skipped` alongside whitespace spans and never become
  Span vertices, never get propositionized, never enter any index.

Net effect: ingest a 60-page incident report, the system quietly keeps the first
~20 pages and discards the rest, reporting success. This violates principle 4
(traceability) and principle 9 (auditability) at the perception layer — the
worst possible place — and no test, log, or Action record surfaces it.

**Fix (code, immediate):** raise or hard-warn when tokenization truncates
(compare token count to input length before dropping `offset_mapping`), and
treat a zero-vector span distinct from a whitespace span — one is a skip, the
other is corruption.

**Fix (architecture):** §1 "embed once" has **no long-document strategy at
all**. Late chunking requires the whole document in the embedding context;
bge-m3 gives 8,192 tokens. The standard remedy is windowed late chunking
(overlapping macro-windows, e.g. 8k with 1–2k overlap, stitched at window
boundaries; "long late chunking"). This must be specified in §1 and tracked as a
Phase 1 gap item (it is currently in no `gap_*.md` / `todo_*.md`). Until then,
design principle 2 is only true for short documents.

### C2. SQL injection / ingest breakage via `$$` in document-derived text

`src/iknos/db/age.py:58` — `cypher()` wraps the Cypher body in PostgreSQL
dollar-quoting: `SELECT * FROM cypher('…', $$ {query} $$)`. `cypher_map()`
escapes single quotes and backslashes (`age.py:31`) but **nothing protects the
`$$` delimiter itself**. Document-derived text flows into that body:

- proposition text (LLM-rewritten from source documents) at
  `core/proposition.py:426-428`;
- document title at `core/ingest.py:373-374`;
- span `layout` JSON at `core/ingest.py:278-279`.

Any ingested text containing `$$` (price tables, code, LaTeX, shell snippets —
common in technical evidence) terminates the dollar-quoted string. Everything
after it is parsed as **raw SQL on the ingest connection**. Best case: ingest
crashes mid-transaction on legitimate documents. Worst case: a crafted document
executes SQL — a real concern for a system whose threat model already includes
adversarial sources (§9.1).

**Fix:** use a randomized/unique dollar tag (`$ik_<nonce>$ … $ik_<nonce>$`) and
reject bodies containing the tag; or pass properties through AGE's third
`parameters` argument (prepared-statement agtype params) instead of inlining.
Add a regression test that ingests text containing `$$`, `$tag$`, quotes, and
backslashes.

### C3. No ANN index on either vector table — the §5.1 funnel's workhorse is a sequential scan

Migrations `0002` and `0003` create `document_embeddings` and
`proposition_embeddings` with btree/GIN indexes only — **no HNSW or IVFFlat
index exists anywhere**. Architecture §5.1 calls embedding k-NN "the workhorse
stage… sublinear (approximate NN)", and §6.1 amortizes a "large but static
reference corpus" through it. Without an ANN index every k-NN is a full scan;
this works in tests and degrades linearly with the reference corpus — precisely
the silent-at-first, fatal-at-scale failure class. No phase doc owns creating
the index, choosing HNSW parameters (`m`, `ef_construction`), or deciding
operator class (cosine vs ip for normalized vectors).

**Fix:** add an HNSW index migration before Phase 2 retrieval work; record the
distance operator decision (vectors are L2-normalized, so inner product and
cosine are equivalent — pick one and standardize).

### C4. AGE `MERGE` on unindexed properties — ingest writes become per-row label scans

Every graph write resolves a vertex by property equality:
`MERGE (s:Span {id: '…'})` (`ingest.py:279`), `MATCH (p:Proposition {id: …})`
(`proposition.py:433`). Apache AGE does **not** index vertex properties by
default; each such MERGE/MATCH scans the label's table. Persisting *n* spans is
then O(n²) per document, and proposition→span edge creation pays the same scan
twice. This compounds with §13's own admission that AGE under this property
density is "a real viability risk… benchmark it early" — yet the AGE benchmark
sits in Trial C ("not MVP-blocking").

**Fix:** create expression indexes on the `id` property of the hot labels
(`Span`, `Proposition`, `Document`, `Object`) over the AGE storage tables
(btree on `agtype_access_operator(properties, '"id"')` or equivalent) as a
migration now; promote the AGE density/latency benchmark from Trial C to a
Phase 2 entry gate.

---

## 2. High — architecture gaps and plan mistakes

### G1. "Embed once, cache" is not actually implemented — and the plan doesn't notice

Principle 2 and §1 promise cached **contextualized token embeddings** from which
"every granularity" derives. The code caches token embeddings only in memory for
the duration of one ingest; the `document_embeddings` table stores *pooled
span vectors* (1024-d per span), not token embeddings. Consequences the plan
doesn't acknowledge:

- Multi-level segmentation (G1.10) and any future re-pooling will **re-embed
  the document**, the exact L× cost §1 claims to avoid.
- The resegmentation guard (`DocumentResegmentationError`) makes this safe but
  not cheap.

Persisting token embeddings is ~33 MB/document (8k × 1024 × fp32), which may be
the right trade — but it should be a *decision*, recorded in §1 or
`gap_phase_1_ingest.md`, not a drift between principle and code. Options:
persist token embeddings (fp16 halves it), or amend principle 2 to "embed once
per pipeline version" and accept re-embedding on level changes.

### G2. No concurrency or isolation model anywhere in the design

§6 describes async operators streaming results into one shared AGE graph; §11
describes a loop where expand/adjudicate/revise run repeatedly; transactions are
caller-owned (`ingest.py:21-24`). Nowhere — architecture or phase docs — is
there a story for: two operators writing overlapping subgraphs; Layer A/B
recomputation racing an in-flight extraction; two investigations sharing
reference boxes while one deprecates a box; or expert overrides landing during
adjudication. Postgres MVCC gives row-level consistency, not reasoning-level
consistency (a QBAF recompute reading half of a multi-edge write is wrong but
never errors). This shapes the Phase 3 Layer A/B interface, so deciding it late
forces rework.

**Fix:** specify the cheap version now — e.g. *single writer per working box*
(serialize operator writes through one queue per investigation), reads at
`REPEATABLE READ`, recompute triggered post-commit. That one sentence in
architecture.md closes most races and costs nothing at investigation scale.

### G3. The go/no-go gate is structurally late — and the plan calls it "early"

`todo.md` and `todo_trials.md` describe E1 (beat plain RAG / agentic RAG /
expert+search, else "stop and rethink") as an *early* go/no-go. But E1 requires
extract → link → adjudicate → answer, i.e. **Phases 2, 3, and 4 substantially
built** — the most expensive engineering in the project (entity resolution,
two-layer propagation, QBAF, edge-judgment pipeline). If E1 fails, the
"stop and rethink" verdict arrives after the majority of the build cost is
sunk. This is the single biggest plan-level mistake.

**Fix:** add an **E1-lite** after Phase 2: propositions + entity resolution +
hybrid retrieval, no reasoning layer, evaluated on the differentiator axes that
don't need adjudication (traceability, contradiction *surfacing* via NLI on
retrieved pairs, calibration of faithfulness). It is a genuinely cheap
approximation of the RAG baseline comparison and can kill or redirect the
project two phases earlier. Relabel full E1 honestly as a mid-project gate.

### G4. Interface-shaping decisions are filed as non-blocking benchmarks

Two items classified under Trial C ("scale/latency, not MVP-blocking") in fact
gate earlier phases:

- **C1, re-evaluation trigger policy (eager vs lazy)** — §13 itself says "it
  shapes the Layer A↔B interface, so decide before hardening Layer B." Phase 3
  can currently be marked done without it, and Phase 5 then discovers the
  interface is wrong. Move to a Phase 3 entry/exit gate.
- **AGE density benchmark** — §13 says "benchmark it early"; the trials file
  says not blocking. With C4 above, this should run at Phase 2 entry with the
  property indexes in place, on a synthetic graph at 10–100× the expected
  investigation size.

### G5. Phase 2 rests on three subsystems no document specifies

Every Phase 2+ doc assumes: (a) an **entity linker** (anchoring mentions to
domain-pack taxonomies — model? gazetteer? threshold?); (b) **meronymy
induction** (algorithm unstated: regex? parser? LLM?; triage-load unsized);
(c) **credibility derivation** (who computes `reliability_prior ×
interest_alignment`, when, cached where, what recomputes on change). All three
are foundational to graph construction; none has a design, an estimate, or a
gap-doc entry. The same applies one phase later to the **QBAF solver** —
"in-house implementation, QBAF-Py as reference only" is a research-and-build
task hiding inside one Phase 4 bullet.

**Fix:** a short `gap_phase_2_prereqs.md` with a design + prototype spike for
each (1–2 weeks total), before Phase 2 proper starts. Budget Phase 4 with an
explicit QBAF-solver sub-task including oscillation-detection tuning.

### G6. Bitemporal semantics: supersession has no trigger and time never enters reasoning

§7.4 and Phase 5 define the *record* (event time, ingestion time, validity
windows) but not the *semantics*: what makes fact F2 supersede F1 (QBAF
contradiction? explicit correction? expert action?); whether Layer A retracts
immediately when `valid_to` is set or only on a revision trigger; how two facts
with overlapping event-time windows coexist. The ensemble gate's "temporal
agreement" check (§7.2) is similarly undefined. These are cheap to write down
now and expensive to retrofit after Phase 3 freezes the Layer A interface.

### G7. `provisional` is one flag carrying at least three meanings

Low faithfulness (Phase 1, §3.1), unresolved reference binding (Phase 2, §3.1),
and not-yet-re-inferred-under-budget (Phase 5, §6.1) all set the same boolean.
The quarantine gate (block high-stakes `REFUTES`) reads the flag without the
reason; triage (§11.1) explicitly needs the reason to tell the expert *what
judgment is needed*. Make it `provisional_reasons: set[str]` now, while the
schema is young — the migration is trivial today and painful after Phase 2.

Related, already-tracked but worth elevating: **the quarantine gate itself is
not enforced** (G1.6 — `is_provisional()` exists, nothing calls it). Until
Phase 2 edge-creation lands, every low-faithfulness proposition flows
downstream unimpeded. Keep G1.6 at the top of the Phase 2 entry list.

---

## 3. Medium — performance and usability gaps

### P1. Embedding inference runs in-process; there is no serving or queueing story before Phase 6

`EmbeddingSubstrate` loads bge-m3 into the calling process (torch, CPU by
default) — inside the API container per `compose.yaml`. An 8k-token forward
pass on CPU takes tens of seconds; multi-sample extraction multiplies LLM calls
per span (N samples + verify per proposition). The architecture defers the task
queue to Phase 6 (§6 "orchestration uses an open-source task queue"), but
real-corpus ingest needs background jobs, retry, and backpressure from
Phase 2 at the latest. Recommendations: serve embeddings the way the LLM is
already served (TEI or vLLM embedding endpoint behind the existing swappable
seam), and pull the task-queue decision (arq/Celery/Hatchet — anything
self-hosted) forward to Phase 2. Otherwise the first real ingest of 30 documents
is an hours-long, unrecoverable, single-process foreground job.

### P2. Hybrid retrieval fusion is unowned

§4 mandates dense + sparse hybrid retrieval; no document says how results are
fused (RRF? weighted sum? rerank?). It's a small decision but it sits on the
critical path of Phase 2 retrieval and the §5.1 funnel, and "the knob nobody
owns" is how it ends up hardcoded ad hoc. One line in
`todo_phase_2_graph_construction.md` (e.g. "RRF, k=60, then optional
cross-encoder rerank behind the swappable seam") closes it.

### P3. No observability plan for an LLM-heavy pipeline

The Action log records *what* happened, not *how long*, *how much*, or *why it
failed*. There is no metrics/tracing story (per-operator latency, token spend
per document, cache hit rates, verifier disagreement rates). For a system whose
cost discipline (§6.1) and calibration loop (§10.3) are core claims, these
numbers must be collectable from day one — they are also exactly what Trials
A/C need. Cheap fix: structured log fields + a `metrics` JSONB column on
`actions` (duration, tokens, cache_hit), aggregated later.

### P4. No authentication design despite a clearance-based access model

§9.1 builds a sensitivity lattice and clearance-relative auditability; §6
mentions "auth" as a Postgres table; the API is an unauthenticated stub behind
Traefik in `compose.prod.yaml`. Nothing anywhere defines authn (users,
sessions, service identities) or how a "viewer's clearance" is established and
trusted. Phase 7 cannot bolt this on under a security model this load-bearing.
Add an auth design item (even "OIDC via Authentik/Keycloak, clearance claims in
the token") to Phase 6/7 prerequisites.

### P5. Embedding-model migration is undesigned

`document_embeddings` rows don't record the model that produced them (the
segmentation content-hash does, indirectly). Swapping bge-m3 — likely, given
the self-hosting principle and model churn — invalidates every vector and every
consistency-clustering threshold (0.86 cosine in `consistency.py` is
model-specific). A `model_version` column on both embedding tables plus a
stated re-embedding procedure turns a future crisis into a migration.

---

## 4. Lower-severity observations

- **MinerU is a single point of failure with no degraded mode for PDFs.** The
  NullParser fallback only covers plain text. Acceptable for now; consider
  queueing parse jobs (P1) so a MinerU outage delays rather than fails ingest.
- **No timeout on LLM calls** (`core/llm.py` uses the default client timeout) —
  a hung vLLM request stalls a whole document's extraction; set explicit
  timeouts to match the MinerU client's discipline.
- **`consistency.py` greedy clustering threshold (0.86)** is a calibration
  constant with no provenance — fine, but it belongs in the extraction cache
  key (verify it is included; the sampling/model are, the threshold should be).
- **Sensitivity propagation is deferred to "Phase 3/5"** without an owner —
  assign it (eager max-propagation at derived-node creation is the simpler
  choice and must land before Phase 7 visibility).
- **Entity-resolution scope ambiguity** — per-box vs global resolution before
  cross-box `SAME_AS` is left open in Phase 2/6 docs; pick per-box + working-box
  candidates (the conservative reading of §9) and write it down.
- **`todo.md` phase table vs reality drift** — G3.1 (Layer A) shipped in
  parallel with Phase 1 per `HANDOFF.md`; harmless, but the dependency diagram
  says Phase 3 starts after Phase 2. Keep the doc honest or the gates lose
  meaning.

---

## 5. What is in good shape (kept brief, for calibration)

- The propose/dispose invariant, two-layer propagation split (§12), and the
  faithfulness/credibility/strength separation are genuinely well-reasoned and
  consistently carried through schema, plans, and code.
- Phase 0–1 code quality is high: deterministic IDs, content-addressed caches
  keyed on model+prompt+schema versions, immutability guards instead of silent
  re-writes, retry discipline (5xx-only), grammar-level structured output, and
  ~4,100 lines of real tests including DB-backed integration tests.
- Risk awareness in §13 is unusually honest — most findings in this review are
  *sequencing/ownership* failures of known risks, not unknown risks.

---

## 6. Prioritized actions

| # | Action | Where | When |
|---|--------|-------|------|
| 1 | Fail loudly on embedding truncation; stop conflating zero-vector with whitespace skips | `embeddings.py`, `ingest.py` | Now |
| 2 | Randomized dollar-quote tag (or agtype params) + hostile-text ingest test | `db/age.py` | Now |
| 3 | Spec windowed late-chunking for long documents in §1; add gap item | `architecture.md`, `gap_phase_1_ingest.md` | Now |
| 4 | HNSW index migration + distance-operator decision | `alembic/` | Before Phase 2 retrieval |
| 5 | AGE property-id expression indexes + density benchmark as Phase 2 entry gate | `alembic/`, `todo_trials.md` | Before Phase 2 |
| 6 | Write the concurrency model (single writer per working box) into §6 | `architecture.md` | Before Phase 3 |
| 7 | Reclassify trigger-policy trial (C1) as Phase 3 gate | `todo_trials.md`, `todo_phase_3` | Before Phase 3 |
| 8 | Add E1-lite after Phase 2; relabel E1 as mid-project | `todo.md`, `todo_trials.md` | Plan change, now |
| 9 | `gap_phase_2_prereqs.md`: entity linker, meronymy induction, credibility derivation design spikes | new doc | Before Phase 2 |
| 10 | `provisional` → reasons set; enforce G1.6 quarantine | schema + Phase 2 | Phase 2 entry |
| 11 | Serve embeddings out-of-process; pull task-queue decision into Phase 2 | infra | Phase 2 |
| 12 | Supersession-trigger + temporal-reasoning semantics for §7.4 | `architecture.md`, Phase 5 doc | Before Phase 3 freeze |
| 13 | Metrics fields on `actions`; auth design item for Phase 6/7 | various | Opportunistic |

---

*Method: full read of `architecture.md` and `todo.md`; two parallel exploration
passes over (a) `src/`, `tests/`, `alembic/`, infra and (b) all phase/gap/trial
docs; direct verification of every critical finding against source
(`embeddings.py`, `ingest.py`, `age.py`, `proposition.py`, migrations 0001–0006).*
