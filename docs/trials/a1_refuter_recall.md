# Trial A1 — Candidate-generation refuter-recall harness (scaffolding)

**Instrument A / Trial A1** (`docs/todo_trials.md`) — *⚠ may force redesign*. Gates Phase-4 candidate generation and Phase-6 generate-candidates. **This is the LLM-free, label-free, DB-free harness scaffolding; the live measurement is pending (needs the gate corpus ingested into a DB + the embedding/LLM services up — §4).** No recall number below describes the corpus.

- **Harness:** `scripts/a1_refuter_recall.py` (reproduce: `uv run python -m scripts.a1_refuter_recall`); scorer `iknos.trials.a1_recall` (pure, unit-tested); funnel under test `core/candidates.py` (read-only).
- **Decision threshold (recorded):** adopt the smallest candidate budget recalling ≥ 90% of planted **refuters** (§5.1 binding constraint); the live run fills in the achieved value.

## 1. Gold inventory (real — from the V1 planted manifest, no V2 labels)

The planted `supports`/`refutes` cross-references in `tests/fixtures/gate_corpus/manifest.toml` are the planted edge ground truth A1 scores recall against. **3 supporter**, **2 refuter** (2 dissimilar) planted edges.

| planted edge | sign | dissimilar |
| --- | --- | --- |
| d03-self-serving-judgement → H2 | supports | no |
| dissimilar-refuter-overload → H4 | refutes | yes |
| d06-admission-against-interest → H1 | supports | no |
| dissimilar-refuter-counterfeit → H3 | refutes | yes |
| load-bearing-tail-fact → H1 | supports | no |

The dissimilar refuters are the §5.1 binding subset: embedding k-NN under-generates them (a refuter can be semantically far from its target), so the structural prior is the recall floor that must catch them — exactly what the live measurement tests.

## 2. Harness path

`build_gold_edges(manifest)` → run the funnel (`core/candidates.py`) over the active subgraph → `pool_to_node_pairs` → `a1_recall.project_to_gold` (node-id space → planted-id space, via the ingest's anchor→node and hypothesis→node maps) → `a1_recall.score_recall` (supporter / refuter / dissimilar-refuter recall, split). Budget is applied in **node space** (the funnel's `k` / rank order is the cost knob); the projected gold-space recall is read at each budget. Cost is the node-space pool size, not the projected count.

## 3. Synthetic wiring demonstration (illustrative — NOT the gate measurement)

The real funnel run on a **synthetic** active subgraph whose geometry reproduces the §5.1 case: each supporter is embedding-near its hypothesis; each dissimilar refuter is embedding-**orthogonal** to its hypothesis but shares an `INVOLVES` entity with it; distractors fill the top-k. The embeddings are hand-built, so these numbers reflect the *synthetic* similarity, **not** the corpus — they show only that the harness runs against the real funnel and discriminates the two funnel strategies.

| funnel | candidates | supporter recall | refuter recall | dissimilar-refuter recall |
| --- | --- | --- | --- | --- |
| embedding-knn only | 12 | 1.00 | 0.00 | 0.00 |
| union (structural ∪ embedding) | 14 | 1.00 | 1.00 | 1.00 |

As designed: the embedding-only funnel recalls supporters (1.00) but **misses the dissimilar refuters** (0.00); the recall-first **union** recovers them via the structural prior (1.00) — the §5.1 mitigation the live A1 run quantifies on real embeddings. The harness measures it.

## 4. Live-run recipe (deferred — needs a DB + embedding/LLM services up)

Not run here (host policy: no containers started without approval; vLLM is down). To produce the real A1 numbers when the services are up:

1. Ingest the gate corpus (d01–d10) into an **isolated** ephemeral database (`CREATE DATABASE` per the C3 pattern) — perception + extraction, so the funnel has real `proposition_embeddings` and `INVOLVES` edges. *(Extraction needs vLLM **and** R11-H merged — see the R11-H gate.)*
2. Build the two projection maps from the ingest: planted-anchor → reasoning-node id (locate each planted quote's span → its `EVIDENCED_BY` reasoning node) and hypothesis label → `Hypothesis` node id.
3. Sweep the funnel knobs (embedding `k`, `FunnelStrategy`, `min_similarity`) via `CandidateGenerationAdapter.generate`; for each, `project_to_gold` + `score_recall`.
4. Record the recall-vs-cost curve; the decision is the smallest budget reaching the 90% refuter-recall target. **Redesign trigger (§5.1):** if similarity + entity/topic generation still cannot recall the dissimilar refuters, contradiction-finding must become a dedicated pass over the hypothesis neighbourhood, not a funnel-gated step.

## 5. Status & gating

- **Scaffolding complete, run pending.** Gold inventory, scorer, projection and the funnel-wired harness are committed and unit-tested; the measurement awaits a live DB + embedding service (and, for the extraction half, vLLM + R11-H).
- **No labels required** — gold is the V1 planted manifest; this does not touch the V2 label families (which do not exist yet).
- **A1 is ⚠ may-force-redesign** — the dissimilar-refuter recall is the result that can trigger the §5.1 redesign; do not harden Phase-4 candidate generation on it until the live run lands.

