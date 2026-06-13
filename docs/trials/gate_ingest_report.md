# Gate-corpus dry-run ingest — run report (Trial C / §6.1)

- corpus: `tests/fixtures/gate_corpus` (10 documents); box: `gate-corpus`; graph: `iknos`
- embedding backend: **in-process bge-m3** (`BAAI/bge-m3`) — the R11 worker builds `EmbeddingSubstrate` in-process; the R10 `make_embedding_backend` out-of-process seam is **not yet wired into the ingest job**, so `EMBEDDINGS_BASE_URL` (`unset`) had no effect on this run
- queue (R11): `ingest:gate-corpus` — per-box execution lock, one worker (concurrency 1)

**Pipeline scope.** The R11 ingest job runs perception only (parse → segment → embed → persist `Span`s + index rows). Propositionization is a separate operator, not wired into the queue; proposition / faithfulness counts below are whatever it produced separately.

## Totals

- enqueued jobs: **10**
- spans (graph `Span` vertices): **77**
- propositions (graph `Proposition` vertices): **0** _(0 — propositionization not run; see scope note)_
- R12 Action metrics: **present** — total ingest wall-clock **82255 ms** (per-document below, summed parse+segment `metrics.duration_ms`); token/cost keys are LLM-stage and absent from this perception-only ingest

## Per-document

| doc | spans | embedding rows by level | doc windows | ingest ms | propositions | faithfulness set | provisional |
|-----|------:|-------------------------|------------:|----------:|-------------:|-----------------:|------------:|
| d01 | 4 | L0:2, L1:2 | 1 | 804 | 0 | 0 | 0 |
| d02 | 6 | L0:3, L1:3 | 1 | 714 | 0 | 0 | 0 |
| d03 | 6 | L0:3, L1:3 | 1 | 1039 | 0 | 0 | 0 |
| d04 | 4 | L0:2, L1:2 | 1 | 922 | 0 | 0 | 0 |
| d05 | 4 | L0:2, L1:2 | 1 | 799 | 0 | 0 | 0 |
| d06 | 6 | L0:3, L1:3 | 1 | 887 | 0 | 0 | 0 |
| d07 | 8 | L0:4, L1:4 | 1 | 738 | 0 | 0 | 0 |
| d08 | 31 | L0:16, L1:15 | 3 | 74726 | 0 | 0 | 0 |
| d09 | 4 | L0:2, L1:2 | 1 | 964 | 0 | 0 | 0 |
| d10 | 4 | L0:2, L1:2 | 1 | 662 | 0 | 0 | 0 |

## d08 multi-window check (G1.13)

- ✅ d08 segmented under **3** document-level embedding windows (from the segment `Action`'s `inputs.windowing.count`) — the >8,192-token purchasing record spans more than one window, so the load-bearing tail fact in its final 10% is covered (the path the multi-window embedding exists to serve).

## Extraction (propositionization) — attempted, failed (expected: LLM down)

_(Appended to the script's perception sanity-read, which counts graph propositions but not job
outcomes.)_ The ingest job chains a `propositionize_document_job` per document (#108
perception→extraction split). All **10** chained extraction jobs **failed** with
`ValueError: No LLM model configured` — the configured vLLM (`192.168.0.247:8000`) is unreachable
and `LLM_MODEL` is unset. This is the documented degraded mode, not a regression: propositionization
*is* the LLM step, so it cannot run offline, which is why propositions / faithfulness / provisional
are all 0 above. Perception (parse → segment → embed → persist) is LLM-free and committed fully.

## Run provenance

- Run **2026-06-13** against a **throwaway isolated database** (`iknos_gate_dryrun` on the shared
  ephemeral server), migrated to head + a separate `procrastinate schema --apply`, and **dropped
  after** — the shared `iknos` graph and the other lane's job queue were never touched.
- Two core bugs were surfaced by this dry run and fixed to reach the numbers above (accompanying
  core-fix PR): (1) the in-process embedding crashed under the locked **`transformers 5.9.0`**,
  which removed the fast-tokenizer `build_inputs_with_special_tokens` / `get_special_tokens_mask`
  the windowing path relied on; (2) the queued perception worker **never committed** (a
  caller-owned-transaction with no `atomic_write`), so every ingest job reported success while
  persisting nothing. The figures here are from the fixed path.