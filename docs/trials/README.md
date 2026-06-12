# `docs/trials/` — trial outputs and protocols

Artifacts the validation-gate trials (`docs/todo_trials.md`) produce or are run from. The
**E1 baseline ladder** writes its answer sets here, under the shared `BaselineAnswer` contract
(`src/iknos/baselines/contract.py`), so the V3 metrics harness scores every rung identically.

## Baseline answer files (generated)

| file | rung | produced by |
|------|------|-------------|
| `baseline_rag_answers.toml` | V4 — plain RAG | `scripts/run_baseline.py --baseline rag` |
| `baseline_agentic_answers.toml` | V5 — agentic / multi-hop RAG | `scripts/run_baseline.py --baseline agentic` |
| `e1_expert_answers.toml` | V6 — expert + search | a human, from `e1_expert_answers_template.toml` |

These are **generated artifacts** — produced by a run against a live DB + LLM endpoint and not
committed (they depend on the model and the corpus snapshot). Regenerate with:

```
uv run python -m scripts.run_baseline --baseline rag \
    --corpus tests/fixtures/gate_corpus \
    --questions tests/fixtures/gate_corpus/questions.toml
```

## The contract

Each file has a `[meta]` header (how the run was produced — rung, corpus, models, tuning) and
one `[[answers]]` block per question: `question_id`, `answer_text`, `cited_chunk_ids`,
`confidence`. A rung that cannot answer a question records it under `[[unanswered]]` (loudly —
a silently dropped answer would bias the score). The questions and their differentiator axes are
`tests/fixtures/gate_corpus/questions.toml`; the gold answers they are scored against are Trial
V2 (`tests/fixtures/gate_corpus/labels/`).
