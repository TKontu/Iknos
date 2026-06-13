# Gate-corpus dry-run ingest — runbook (Trial C / §6.1)

The reproducible recipe for `scripts/run_gate_ingest.py`: ingest all ten
`tests/fixtures/gate_corpus/` documents through the **real R11 job queue**, then sanity-read the
result into a report under `docs/trials/`. The R11 ingest job embeds **in-process** (it constructs
`EmbeddingSubstrate` directly); the R10 out-of-process embedding seam is not yet wired into the job
(see Prerequisites).

## Status: RUN — perception (2026-06-13). Extraction deferred (LLM down)

Full result + provenance: `docs/trials/gate_ingest_report.md`. Outcome in one line: **perception
ingest committed — 77 `Span` vertices across the 10 documents, d08 segmented into 3 embedding
windows (the G1.13 tail-coverage check passes), R12 per-document ingest metrics populated**;
**extraction (propositionization) was attempted and failed loudly** with `No LLM model configured`
on all 10 chained jobs (the LLM is offline) — the documented degraded mode, so 0 propositions.

The run targeted a **throwaway isolated database** (`iknos_gate_dryrun` on the shared ephemeral
server — separate AGE graph **and** separate `procrastinate_*` tables), migrated to head +
`procrastinate schema --apply`, and **dropped afterward** — the shared `iknos` graph and the other
lane's queue were never touched (so the contamination concern below is satisfied without a new
container; the `iknos` role has `CREATEDB`).

Two core bugs blocked the perception path and were fixed before this run produced anything (see
the core-fix PR): the in-process embedding crashed under the locked `transformers 5.9.0`, and the
queued worker never committed its writes (caller-owned transaction with no `atomic_write`). With
those fixed, perception runs offline end-to-end; only extraction still needs the LLM back.

Standing constraints that shaped the run (kept for the next executor):

- **The LLM endpoint is unreachable** (`LLM_BASE_URL` / the configured vLLM at
  `192.168.0.247:8000` did not respond; `LLM_MODEL` is unset). The R11 ingest job itself does
  *not* call the LLM — it is parse → segment → embed → persist spans — so ingest runs on the
  embedding backend alone. The proposition / faithfulness / provisional distribution still needs
  the LLM back; re-run step 3 below once it is.
- **Do not contaminate the shared integration DB.** The only standing database here is the
  long-lived ephemeral container shared with the other lane's integration tests (see the project
  memory *Ephemeral DB recipe*). Ingesting into its production `iknos` graph risks the documented
  stale-state failures in the other lane — so this dry run used a dedicated throwaway DB on the same
  server (no new container needed; `CREATE DATABASE` + drop), per the recipe below.

Offline wiring was validated without a DB: `uv run python -m scripts.run_gate_ingest --plan` lists
the ten documents with their deterministic `uuid5` ids (d08 is ~59 KB → comfortably over the
8,192-token single-window floor, so the multi-window path will exercise).

## Pipeline scope (a finding, not a caveat)

The R11 queue task `iknos.jobs.app.ingest_document_bytes_job` runs **perception**: parse →
segment → embed → persist `Span` vertices + dense (`document_embeddings`) / sparse index rows.

> **Update (2026-06-13): #108 landed both follow-ups below — propositionization IS now queued and
> chained.** `ingest_document_bytes_job` chains a `propositionize_document_job` per document on
> success (perception→extraction split), and `core.ingest.load_document_spans` is the span-reloader.
> The 2026-06-13 run observed all 10 extraction jobs fire automatically after their ingest — they
> just **fail with `No LLM model configured`** while the LLM is offline (the expected degraded mode),
> so propositions stay 0 until the LLM is back. No in-process step is needed anymore; bring the LLM
> up and the chained jobs (or a re-fired chain) fill the proposition columns. *(Historical context
> of the two follow-ups, now both shipped:)*

1. a `propositionize_document_job` queue task (mirrors the ingest task) so the queue covers
   perception **and** extraction, and
2. a `list[Span]`-by-`document_id` reader, so propositionization can run post-ingest from the graph
   rather than only from an ingest call's return value.

## Prerequisites

- **Database** at Alembic `head` (creates the AGE graph + label indexes) **plus the procrastinate
  schema applied separately**: `procrastinate --app=iknos.jobs.app.app schema --apply`.
  *Correction (2026-06-13): alembic head does **not** create the `procrastinate_*` tables — the
  earlier "both are present in a head DB" claim was wrong. A fresh head DB has zero `procrastinate_*`
  tables until the schema-apply step runs (this is why `compose.yaml`'s migrate step and
  `MIGRATIONS.md` run **both** commands). Skip it and enqueue fails with
  `type "procrastinate_job_to_defer_v1[]" does not exist`.*
- **Embedding backend: in-process bge-m3.** The R11 ingest job (`iknos.jobs.app`) constructs
  `EmbeddingSubstrate` in-process (torch in the worker; CPU works but is slow for d08's many
  windows), reloading the model per document. **`EMBEDDINGS_BASE_URL` is not yet effective for this
  run**: the R10 `make_embedding_backend` out-of-process seam is not wired into the ingest job
  (`core.ingest` does not yet take the backend protocol), so setting it has no effect until the core
  lane wires it in. The report header states which backend the worker actually used.
- **No LLM needed for ingest itself**; needed only for the (separate) propositionization step.

## Recipe (against a fresh, isolated DB)

```bash
# 1. A throwaway, isolated DB. Either a new container, OR (no container needed — what the
#    2026-06-13 run did) a throwaway database on the shared server: the iknos role has CREATEDB, so
#    `CREATE DATABASE iknos_gate_dryrun` (drop it after) gives a separate AGE graph AND separate
#    procrastinate tables without touching the shared `iknos` DB. Apply BOTH schemas — alembic does
#    NOT create the procrastinate job tables:
export DATABASE_URL=postgresql+asyncpg://iknos:change-me@iknos_pg_ephemeral:5432/iknos_gate_dryrun
.venv/bin/alembic upgrade head
.venv/bin/procrastinate --app=iknos.jobs.app.app schema --apply

# 2. Dry run: enqueue all 10 docs (per-box queue + box lock + per-doc queueing lock), drain with
#    one worker (concurrency 1 → the §6 per-box serialization), then sanity-read + write the report.
DATABASE_URL=$DATABASE_URL .venv/bin/python -m scripts.run_gate_ingest \
    --corpus tests/fixtures/gate_corpus --box gate-corpus \
    --out docs/trials/gate_ingest_report.md

# 3. (Until propositionization is queued) run it in-process to fill the proposition columns, then
#    re-run the sanity-read. Needs LLM_MODEL + a reachable LLM_BASE_URL (and LLM_VERIFIER_MODEL for
#    faithfulness). See scripts/run_gate_ingest.py's "Pipeline scope" docstring.
```

## What the report contains

- per-document **span** counts (graph `Span` vertices) and **embedding rows by level**
  (`document_embeddings`), plus the document-level **window count** read from the segment `Action`'s
  `inputs.windowing.count` (the layout the pipeline records — *not* `span_id IS NULL` rows, a slot
  the pipeline never writes);
- the **d08 multi-window check** (G1.13): d08 must show > 1 document window — the load-bearing tail
  fact lives in the final 10% of its >8,192-token text;
- **proposition / faithfulness / provisional** counts (zero until propositionization runs);
- **per-document ingest duration** from R12 Action metrics. R12 (#103) **has merged**: it added the
  `actions.metrics` JSONB column with `duration_ms` as a *key* (not a top-level column), so the
  runner sums each document's parse+segment `metrics.duration_ms` into the report. Token/cost keys
  are LLM-stage and absent from this perception-only ingest.

## Fixing what the dry run breaks

That is the dry run's purpose. Anything that breaks in `src/iknos/core/*` beyond a trivial fix is
**reported to the core-owning lane, not improvised** here (this lane owns `scripts/` + `docs/trials/`
only). The two scope follow-ups above are the first such reports.
