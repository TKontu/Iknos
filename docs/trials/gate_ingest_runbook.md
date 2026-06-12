# Gate-corpus dry-run ingest — runbook (Trial C / §6.1)

The reproducible recipe for `scripts/run_gate_ingest.py`: ingest all ten
`tests/fixtures/gate_corpus/` documents through the **real R11 job queue** + **R10 embedding
seam**, then sanity-read the result into a report under `docs/trials/`.

## Status: NOT YET RUN (2026-06-12)

The runner and this runbook are landed; the dry run **has not been executed**. Why, plainly:

- **The LLM endpoint is unreachable** (`LLM_BASE_URL` / the configured vLLM at
  `192.168.0.247:8000` did not respond; `LLM_MODEL` is unset). The R11 ingest job itself does
  *not* call the LLM — it is parse → segment → embed → persist spans — so ingest could run on the
  embedding backend alone. But the §6.1 / Trial C sanity-read also wants the proposition /
  faithfulness / provisional distribution, and **propositionization is the LLM step** (a separate
  operator, below), so the *full* report cannot be produced until the LLM is back.
- **Do not contaminate the shared integration DB.** The only live database here is the long-lived
  ephemeral container shared with the other lane's integration tests (see the project memory
  *Ephemeral DB recipe*). Ingesting ten documents into its production `iknos` graph mid-iteration
  risks the documented stale-state failures in the other lane. The dry run should target a **fresh,
  dedicated** DB (recipe below), and starting a new container needs per-invocation approval (the
  *no docker compose up* discipline) — not taken this session.

Offline wiring was validated without a DB: `uv run python -m scripts.run_gate_ingest --plan` lists
the ten documents with their deterministic `uuid5` ids (d08 is ~59 KB → comfortably over the
8,192-token single-window floor, so the multi-window path will exercise).

## Pipeline scope (a finding, not a caveat)

The R11 queue task `iknos.jobs.app.ingest_document_bytes_job` runs **perception only**: parse →
segment → embed → persist `Span` vertices + dense (`document_embeddings`) / sparse index rows.
**Propositionization (`core.proposition.Propositionizer`) is not wired into the queue** and there is
no document span-reloader on the read path, so the dry run produces spans + embeddings but **zero
propositions** until propositionization is run separately. Two clean follow-ups (both `core/`, owned
by the other lane — reported, not done here):

1. a `propositionize_document_job` queue task (mirrors the ingest task) so the queue covers
   perception **and** extraction, and
2. a `list[Span]`-by-`document_id` reader, so propositionization can run post-ingest from the graph
   rather than only from an ingest call's return value.

Until (1)/(2) land, append an in-process propositionization step after `drain()` to fill the
proposition columns of the report.

## Prerequisites

- **Database** at Alembic `head` (creates the AGE graph + label indexes **and** the procrastinate
  job tables; both are present in a `head` DB — verified: `procrastinate_jobs` et al. exist).
- **Embedding backend (R10).** Either in-process bge-m3 (`EMBEDDINGS_BASE_URL` empty — torch in the
  worker; CPU works but is slow for d08's many windows) or a hosted embedding service
  (`EMBEDDINGS_BASE_URL=http://…`). The worker reloads the model per document today.
- **No LLM needed for ingest itself**; needed only for the (separate) propositionization step.

## Recipe (against a fresh, isolated DB)

```bash
# 1. A throwaway DB on its own network + name, NO published ports (see the ephemeral-DB memory).
#    Requires per-invocation approval. Apply migrations (graph + indexes + procrastinate schema):
export DATABASE_URL=postgresql+asyncpg://iknos:change-me@iknos_pg_ephemeral:5432/iknos
.venv/bin/alembic upgrade head

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
  (`document_embeddings`), plus the count of document-level **windows** (`span_id IS NULL`);
- the **d08 multi-window check** (G1.13): d08 must show > 1 document window — the load-bearing tail
  fact lives in the final 10% of its >8,192-token text;
- **proposition / faithfulness / provisional** counts (zero until propositionization runs);
- **R12 Action cost/duration**: detected reflectively. R12 has **not** merged (the `actions` table
  has no duration/cost columns), so per-document cost/duration are omitted; they are the §6.1 /
  Trial C numbers and will appear once R12 ships and the runner reads the new columns.

## Fixing what the dry run breaks

That is the dry run's purpose. Anything that breaks in `src/iknos/core/*` beyond a trivial fix is
**reported to the core-owning lane, not improvised** here (this lane owns `scripts/` + `docs/trials/`
only). The two scope follow-ups above are the first such reports.
