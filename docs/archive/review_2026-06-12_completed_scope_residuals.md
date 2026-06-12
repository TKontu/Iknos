# Review 2026-06-12 — completed-scope residuals (gate assets + ingest batch)

> **Historical record only — not a task tracker.** The actionable findings are folded
> into the plan as task specs: **G1.25** (`todo_phase_1_ingest.md`), **V12/V13**
> (`todo_trials.md`), **V14** (`todo_phase_4_linking_adjudication.md`). Code
> docstrings citing this file resolve here.

Scope: the work landed since the 2026-06-11 architecture assessment was folded —
PRs #80 (V1), #81 (V3), #82 (W3), #83 (G1.21), #84 (V4), #85 (G1.20), #87 (V6),
#88 (V9), #91 (V5), #92 (G1.22+G1.24+G1.23). Method: four independent reviewers,
one per lane, each instructed to verify every suspicion against the written spec
and the actual code/tests before reporting (no speculative findings). Baseline at
review time: 898 unit tests passing, ruff + mypy clean, single migration head
(`0014_baseline_chunks`).

**Overall verdict: the landed scope is substantially sound and spec-faithful.** No
missing spec items; no blockers. Four medium-severity residuals and a tail of
low/nit items, all confirmed with file:line evidence.

## Findings → owning task

| # | Sev | Finding | Task |
|---|-----|---------|------|
| 1 | M | `rag.py` re-ingest leaves stale tail chunks (upsert on `(document_id, chunk_index, model)` never deletes beyond the new count, `rag.py:222-235`) and `retrieve()` filters only on `model` (`rag.py:251`) — chunks from any previously ingested corpus contaminate retrieval, get cited, and skew E1 | V12 |
| 2 | M | Baselines pass `sampling=None` (`rag.py:187`, `agentic.py:157`) where every other LLM consumer pins temperature 0.0 — E1 answers/confidences vary run to run; the answers-file `meta` (`run_baseline.py:108-116`) does not record the sampling regime | V12 |
| 3 | M | G1.22 backfill silently skipped: the verify-stage identity is read from the span's **newest verify `Action`** (`proposition.py:1007-1044`), which survives `_purge_span_propositions`, so propositions re-extracted while the verifier was off (or replayed with `source_verify_sig=None`) are never backfilled once the same verifier returns (`:1072` skip) — stuck `UNASSESSED_FAITHFULNESS` indefinitely. Conservative direction, but defeats the G1.22 promise | G1.25 |
| 4 | M | V9's EXPLAIN test exercises a hand-written simpler query (`test_candidates_knn_pushdown.py:103-113`), not the production statement (`candidates.py:609-617`), which adds a large `proposition_id IN (…)` and a secondary `ORDER BY proposition_id` — whether the real query uses the HNSW index is unverified; and the `IN` post-filter can starve HNSW recall (≪ k rows when the active-evidence fraction is small; `ef_search`/`iterative_scan` unset, mode undocumented) | V14 |
| 5 | L | Agentic budget counted in steps, not LLM calls (`agentic.py:160-181`): retries don't consume budget — worst case 13 calls vs the spec's "≤ 6 LLM calls + 1 answer"; the byte-identical retry is also useless under a deterministic regime | V12 |
| 6 | L | Both import-boundary tests skip relative imports (`node.level == 0` only — `test_baselines_import_boundary.py:36`, `test_trials_import_boundary.py:30`): a `from ..core import …` bypasses the boundary undetected (no current violation exists) | V12 |
| 7 | L | Push-down SQL tie-break (`(distance, proposition_id)` before `LIMIT k`, `candidates.py:613-627`) differs from the exact path's node-id tie-break (`:343`) at the LIMIT boundary — the docstring's "never a superset" subset invariant can be violated on distance ties (identical proposition texts ⇒ identical vectors make ties realistic); `generate()`'s push-down branch (`:535-553`) is dead under test; the subset assertion is weaker than the test setup affords | V14 |
| 8 | L | d07's second anchor quote ("No material or heat-treatment non-conformance was found.", `manifest.toml:283`) lexically matches H3's phrasing — the spec said the dissimilar refuter carries "none of its vocabulary"; d05's *chunk-level* vocabulary (the "3. Duty and loading" header, "load history", "torque … transient" near the anchor) is weaker than the README inventory claims; d02 is 297 words vs the 300 floor; one `labels/INSTRUCTIONS.md` worked example (~line 58) primes a real corpus hypothesis (overload) instead of the fictional domain | V13 |
| 9 | L | G1.24's context-span keying (document-namespaced uuid5 span ids in the key, `proposition.py:1230`) defeats G1.7b cross-document reuse for any span with a non-empty context window — implemented **as specced** ("deterministic on ingest identity"), but the cost was unweighed and `reuse.py:4-19` still advertises cross-document reuse and claims the verifier signature is in the key (it contradicts the shipped code); three more stale docstrings: `proposition.py:148-149`, `verify.py:58-60`, `verify.py:123-124` | G1.25 |
| 10 | L | Verifier sampling regime is not part of `_verify_sig` (`proposition.py:989-1005`) — as specced, pre-existing ingredient gap carried forward; muted by the temperature-0 default. Noted for the next time the sig is touched, no task | — |
| 11 | nit | `contract.py:124-133` `_toml_str` doesn't escape all control characters (a form feed in an LLM answer would break V3's `tomllib` parse); `run_baseline.py:103` logs "answered %s" for unanswered questions | V12 |
| 12 | nit | `docs/todo.md` still marked V9 "next" though it shipped (#88) — fixed in this PR; `HANDOFF.md` (gitignored) is stale G1.8-era | — |

## Verified sound (highlights — what was checked and held)

- **V3 metrics**: every function hand-checked against its standard definition and
  every fixture expected-value independently recomputed (recall@budget incl. dedup,
  Brier, ECE/reliability diagram, Cohen's κ incl. degenerate-marginal convention,
  Spearman ρ incl. tie handling via average ranks, state-flip buckets). `scoring.py`
  faithfully mirrors `core/edge_judge.py::_permutation` and is deterministic and
  content/order-independent. Importable without `DATABASE_URL`; no runners; no
  plotting deps.
- **V1 corpus**: all 21 anchors independently re-verified to occur exactly once;
  d08 measured at **18,584 bge-m3 tokens** (~2.3 windows) with the tail fact past
  the 90% mark; cross-document scenario coherence checked (WTG-14 = CB-GBX-03 is
  deliberate; the VG150→stores→d02 chain coheres); all five `gold_*.toml` are
  genuine empty templates — nothing LLM-generated.
- **V4/V5**: chunking stride/overlap arithmetic traced by hand (no off-by-one);
  citation mapping, cosine-distance/opclass agreement, complete agentic traces on
  all three exit paths, contract round-trips, migration 0014 fully
  CI_MIGRATIONS.md-compliant.
- **V9/R4**: ranking math correct (ascending `<=>` ≡ descending cosine; final
  re-rank mirrors the oracle); G1.16 `WHERE model =` guard present; no injection
  (vector + UUIDs bound via pgvector-sqlalchemy); k-before-mapping can only shrink,
  never add; default off; funnel/structural stage untouched; in-memory oracle
  retained behavior-identical.
- **G1.20/G1.21/G1.23**: `calibrate_agreement` identity seam with range-validated
  curve output (raises, never clamps); formula is `verify × calibrate(agreement) ×
  parse_quality` (not `min`); null faithfulness → `UNASSESSED_FAITHFULNESS` at all
  four finalization sites, persisted, and conservatively quarantined by the R9 gate
  at `Stakes.HIGH`; `require_sampling_diversity` has a single source of truth, no
  bypass, with the edge-judge permutation-diversity exemption documented.
- **G1.22 mechanics** (apart from finding 3): extraction key no longer varies with
  verifier config; `verify_sig` stamped only on fully-clean spans; backfill rewrites
  in place with zero extractor calls/purges; schema versions correctly not bumped.
