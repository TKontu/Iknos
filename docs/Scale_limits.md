● TL;DR

  Iknos is — by explicit, documented design — built for investigation scale: tens of dense case documents per
  investigation against an amortized reference corpus, single-digit users, one Postgres instance. The
  architecture spec states this assumption and derives engineering decisions from it
  (docs/architecture.md:504-505, :518, :1485-1491). So the scaling limits aren't accidents; they're
  deliberate trade-offs with mostly-named "reopen at scale" seams (§13, the DBSP/Feldera path). What blocks
  mega-corpus / enterprise / public hosting falls into four groups, roughly in order of how fundamental they
  are:

  1. Full-recompute reasoning over an in-memory graph — the core algorithm doesn't scale past ~10⁵–10⁶ nodes
  2. LLM-call economics of ingest — cost and wall-clock grow linearly with corpus size, with several per-unit call sites
  3. Single-engine, single-graph storage with no tenancy — one AGE graph, no corpus/user isolation, measured hot spots already at 30k vertices
  4. Single-process runtime — one API process, one worker, per-box serialization, no horizontal scaling story

  Details below, with the evidence.

  ---
  1. The reasoning core assumes the working set fits in memory and recomputes it in full

  This is the deepest limit because it's algorithmic, not operational.

  - Full-graph materialization per evaluation. derivation_adapter.py:236-379 loads every current reasoning node and every DERIVED_FROM edge into Python dicts (MATCH (n:Label) WHERE n.valid_to IS NULL with no box filter — derivation_adapter.py:268, :327), then builds an in-memory DerivationGraph. The QBAF adapter does the same for all evidential edges (qbaf_adapter.py:256-285). There is no per-investigation scoping pushed into Cypher; filtering happens in Python.
  - Full recompute per revision-loop iteration. Each iteration of the composed loop
  (revision_loop.py:182-225) re-runs Layer A truth maintenance over the whole derivation graph, re-runs the Layer B Kleene confidence ascent, and re-solves the entire QBAF to fixpoint — up to 50 outer iterations, each containing the QBAF's own inner convergence loop. There is no incremental update path; the spec defers it to Differential Dataflow/DBSP (architecture.md:1365, :1456-1462) and Trial C2 (the "when does retraction-propagation latency cross the threshold" benchmark) has not been run.
  - In-memory network analysis. Phase 6's planned investigation runtime extracts the working subgraph into igraph/NetworkX in-process for centrality/community analysis. The spec itself says: "This assumes working sets fit in memory — true at investigation scale; if a single graph ever reached millions of nodes, in-database analytics would have to be reconsidered" (architecture.md:504-505).
  - Quadratic candidate generation fallbacks. Embedding k-NN candidate generation has a pgvector push-down seam, but the default in-memory path is a nested loop over hypothesis × evidence × propositions (candidates.py:324-354); entity-resolution blocking is O(bucket²) per shared token (resolve.py:140-156) — a common token ("pump", "bearing") at 100k entities explodes.

  Consequence: a corpus-scale graph (10⁶+ nodes) makes every verdict a multi-GB load plus tens of full graph re-evaluations. This is the part that requires real re-architecture (incremental/differential computation, query-scoped subgraph loading), not tuning.

  2. Ingest cost scales linearly in LLM calls, serialized through narrow gates

  - One LLM call per proposition for extraction (extract.py:293-330), plus propositionizing samples, plus faithfulness verification, plus per-candidate-pair edge judgment, plus N ensemble-gate samples per refutation candidate. No request batching — concurrency is a single asyncio.Semaphore(8) per component (extract.py:287-291, llm.py:68-98).
  - Back-of-envelope: 1M propositions at ~100ms/call through an 8-permit gate is days of wall-clock LLM time per pipeline stage, and that's before edge adjudication, which scales with candidate pairs, not documents. For a hosted service this is also the dominant cost line: per-document COGS is high and contradiction-heavy corpora are pathologically expensive (refutations trigger ensemble multi-sampling and revision-loop
  iterations).
  - Per-span writes, no bulk insert. Ingest persists each span as an individual AGE MERGE + pgvector upsert inside one transaction (ingest.py:336-404); facts likewise (extract.py:405-421). At 1–5ms per Cypher write, a 10k-span document spends tens of seconds in pure write latency.
  - Single hardcoded LLM endpoint (LLM_BASE_URL=http://192.168.0.247:8000/v1, config.py) — one vLLM box is a shared point of contention for all workers; there's no routing, pooling, or failover.

  3. Storage: single AGE graph, no tenancy, measured hot spots already visible

  The Trial C3 benchmark (docs/trials/c3_age_density_benchmark.md, 30k vertices) validated "stay
  single-engine" at investigation scale but measured the cliff edges:

  ┌────────────────────────────────────────────┬───────────────────────┬────────────────────┐
  │                Query shape                 │ Median @ 30k vertices │      Indexed?      │
  ├────────────────────────────────────────────┼───────────────────────┼────────────────────┤
  │ Box-scoped retrieval / MERGE-by-id / as-of │ 0.6–1.6 ms            │ yes                │
  ├────────────────────────────────────────────┼───────────────────────┼────────────────────┤
  │ Variable-length closure (partOf*1..5)      │ 59.5 ms               │ yes (slowest read) │
  ├────────────────────────────────────────────┼───────────────────────┼────────────────────┤
  │ Edge-property filter (SAME_AS.state)       │ 19.7 ms               │ no                 │
  ├────────────────────────────────────────────┼───────────────────────┼────────────────────┤
  │ Supersession edge rewrite                  │ 1,333 ms              │ no — ~1000× slower │
  └────────────────────────────────────────────┴───────────────────────┴────────────────────┘

  The trial itself gates Phase 5 on adding edge-property GIN and bitemporal range indexes before
  "reference-base scale." Beyond indexes:

  - No multi-tenancy at any layer. One global graph name (config.py), all investigations in one graph, embedding tables global with no corpus/tenant column, and the unscoped MATCHes from §1 mean one user's load touches everyone's data. Public hosting needs corpus isolation designed in, not bolted on.
  - Bitemporal append-only growth. Every revision creates a new row version; no as-of indexes yet; no
  archival. Combined with the unbounded action log — every operator writes per-action provenance rows in the hot path (provenance/action_log.py), and audit reach-back is O(facts) × 3 queries per fact (provenance/audit.py:205-229) — storage and audit-query cost grow superlinearly with activity.
  (Provenance-on-everything is a day-0 hard constraint, so this can be partitioned/batched but never
  dropped.)
  - Default connection pool. create_async_engine(...) with no pool sizing (db/session.py:14) → SQLAlchemy's 5+10 default, a hard ceiling of ~15 concurrent connections. Trivial to fix, but emblematic: nothing is tuned for concurrency yet.

  4. Runtime: one process of everything, serialized per box

  - One uvicorn process, one procrastinate worker in both compose files (compose.yaml:53,72,
  compose.prod.yaml), no replicas, no autoscaling, no queue-depth metrics. Procrastinate (Postgres-backed) could scale to multiple workers, but nothing in the deployment does.
  - Per-box advisory lock serializes ingest (jobs/app.py:124-131): all documents for a box ingest strictly sequentially. Throughput per corpus = 1/(single-document ingest time); 1,000 documents into one box at ~5 min each is ~3.5 days regardless of hardware.
  - Embedding model loaded in-process per worker (embeddings.py:220-238) — every worker replica carries the GPU/VRAM cost; the HTTP embedding service (R10) exists as a seam but isn't the hardened default, and DocumentContext holds full token embeddings per document in memory.
  - No backpressure, rate limits, or upload size limits on POST /documents (api/main.py:33-62); large files are read and base64-encoded inside the request handler. Fine for one expert, not for public traffic.

  What's not a problem

  Credit where due: the API correctly defers ingest to a job queue rather than doing work in-request;
  pgvector HNSW indexes exist for both embedding tables; vertex GIN and edge-endpoint indexes are in place and verified used (Trial C3); reembedding batches propositions (128/batch); the LLM client has timeouts and bounded retries; and per-Action operational metrics (R12) give an observability floor. The seams for the scale path are named in the code and spec (pgvector push-down, HTTP embeddings, DBSP migration, edge-property GIN) — this is a codebase that knows where its ceilings are.

  What each scaling target would actually require

  - Mega corpus (10⁶+ documents/facts): the big one is replacing full-recompute reasoning with incremental computation (the §13/DBSP path) and pushing box/investigation scoping into every Cypher query; plus bulk AGE/pgvector writes (UNWIND-style), batched LLM extraction, edge-property + bitemporal indexes, and entity-resolution blocking that isn't O(N²). The §6.1 "amortized reference corpus" discipline helps, but a continuously-ingesting corpus-scale deployment is explicitly called out in the spec as reopening the architecture (architecture.md:1490-1491).
  - Enterprise (many teams, many investigations): multi-worker deployment with rethought per-box locking, explicit pool sizing + PgBouncer, out-of-process embedding as the default, action-log
  partitioning/retention, queue and pool observability, and read-path scoping so investigations don't
  contend.
  - Public hosting (setting aside auth): everything above plus true corpus/tenant isolation in the schema and graph, per-tenant rate limiting and quotas (especially on LLM spend — contradiction-heavy uploads are a cost amplifier), upload streaming/limits, and horizontal API/worker replicas behind the existing Traefik front.

  One caveat for calibration: yesterday's assessment (docs/review_2026-06-11_planned_architecture_assessment.md) concludes the project's current dominant risk
  is that the composed system has never run end-to-end and the validation gate has no assets — i.e., by the project's own build philosophy (thin slice → validate → harden), none of the above should be acted on yet. These are the documented ceilings you'd hit after the validation gate passes, and the worth-recording finding is that most of them are already named seams rather than unknown unknowns — the two genuinely hard ones being incremental reasoning (§13) and tenancy, which is the only one the architecture doesn't currently name at all.
