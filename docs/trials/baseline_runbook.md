# E1 baseline answer runs over the gate corpus — runbook (Trial E1 / V4–V5)

The reproducible recipe for `scripts/run_baseline.py` over `tests/fixtures/gate_corpus/`,
producing the committed answer artifacts the V3 harness will score once V2 gold labels exist.

## Status: NOT YET RUN (2026-06-12)

The runner (`scripts/run_baseline.py`) and the rig logic (`src/iknos/baselines/`) are landed and
V12-hardened (below); the answer runs **have not been executed** because **the LLM endpoint is
unreachable** (`LLM_BASE_URL` / the configured vLLM at `192.168.0.247:8000` did not respond;
`LLM_MODEL` is unset). Both baselines answer via one `core/llm.py` call per question, so neither can
run without it. Embedding (R10) is available in-process (bge-m3, CPU), so the ingest+retrieval half
would work; the answer half cannot.

When the endpoint is back, run the two commands below; the artifacts land under `docs/trials/`.

## Do NOT score, do NOT touch the labels

The answer artifacts produced here **wait for V3 scoring until the V2 gold labels exist** (the V2
labelling is human-led and pending). This lane produces the *answers only*. Do not run the V3 metrics
harness against them yet, and do not read or modify `tests/fixtures/gate_corpus/labels/` — V13 froze
the corpus for labelling and the labels are the answer key (§8 bias control).

## Commands

```bash
export DATABASE_URL=postgresql+asyncpg://iknos:change-me@iknos_pg_ephemeral:5432/iknos
export LLM_MODEL=<served-model-id>           # required; recorded in the answer meta
# (LLM_BASE_URL defaults to the configured vLLM; EMBEDDINGS_BASE_URL empty = in-process bge-m3)

# Rung 1 — tuned plain RAG (V4): fixed-size chunks, top-k cosine, one cited answer call.
.venv/bin/python -m scripts.run_baseline --baseline rag \
    --corpus tests/fixtures/gate_corpus \
    --questions tests/fixtures/gate_corpus/questions.toml \
    --output docs/trials/baseline_rag_answers.toml

# Rung 2 — agentic / multi-hop RAG (V5): the same retriever driven by a search loop.
.venv/bin/python -m scripts.run_baseline --baseline agentic \
    --corpus tests/fixtures/gate_corpus \
    --questions tests/fixtures/gate_corpus/questions.toml \
    --output docs/trials/baseline_agentic_answers.toml
```

(With no `--output`, the rig defaults to `docs/trials/baseline_<baseline>_answers.toml`.)

## V12 regime — what the answer `meta` block records (verified)

The go/no-go is only as valid as the regime is reproducible, so each `AnswerFile.meta` pins the
run's regime. Verified in `scripts/run_baseline.py` + `src/iknos/baselines/rag.py`:

- **`sampling`** — JSON of the pinned sampling regime (default `{"temperature": 0.0}` = greedy);
  without this the baseline confidences drifted run-to-run (V12). Set via `--temperature`.
- **`baseline`, `corpus`, `embedding_model`, `llm_model`** — the rig, the corpus path, and the two
  model identities (same endpoint + embedding model as the system — a *fair strong* baseline, not a
  strawman).
- **`top_k`, `chunk_tokens`, `overlap_tokens`** — the retrieval/chunking regime (`--top-k`,
  `--chunk-tokens`, `--overlap-tokens`); agentic adds **`max_steps`**.
- **Scoped retrieval (V12):** retrieval is scoped to the documents *this rig instance ingested*
  (`RagBaseline._ingested_doc_uuids`), so a second corpus in the shared `baseline_chunks` table
  cannot contaminate a run's citations. This is a correctness property of the run, not a meta field.

## Environment caveat

Same as the gate-ingest dry run (`gate_ingest_runbook.md`): the only live DB here is the shared
ephemeral integration container, and starting a fresh one needs per-invocation approval. Prefer an
isolated DB for the baseline run too, so its `baseline_chunks` rows do not mix with other work.
