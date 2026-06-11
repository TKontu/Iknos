# Archive — superseded gap plans and review reports

**These files are the historical record, not task trackers.** On 2026-06-11 the
documentation was consolidated: every open task and every still-relevant decision
in these files was merged into the live docs, and the originals were moved here
verbatim. Do not execute tasks from this directory — their live home is below.
Code docstrings and old PR bodies that cite a gap file by name (e.g. migration
`0007` citing `gap_phase_0_residual.md` G0.R2, `core/embeddings.py` citing
`gap_review_2026-06.md` R2) resolve here.

## Where the content went

| Archived file | Live home of its content |
|---|---|
| `gap_phase_0_foundations.md`, `gap_phase_0_residual.md` | `../todo_phase_0_foundations.md` → *Build record* (incl. the "reviewed and dismissed — do not re-raise" list) |
| `gap_phase_1_ingest.md` | `../todo_phase_1_ingest.md` → *Build record* + *Open work* (G1.0 remainders, G1.7 cascade, G1.10B, G1.11, G1.12, G1.19) |
| `gap_phase_2_graph_construction.md` | `../todo_phase_2_graph_construction.md` → *Build record & deferred seams* (per-increment seams + the re-scoped R13 design-doc check) |
| `gap_phase_3_reasoning_core.md` | `../todo_phase_3_reasoning_core.md` → *Build record & the G3.3 deferral* |
| `gap_phase_4_linking_adjudication.md` | `../todo_phase_4_linking_adjudication.md` (status block carries the per-slice summary; this archive file keeps the full per-slice design records) |
| `gap_review_2026-06.md` (R1–R13) | Shipped R1–R3/R5–R7: phase *Build records*. Open: R4/R8/R9 → `../todo_phase_4_*.md` *Open task specs*; R10/R11 → `../todo_trials.md` *Gate prerequisites*; R12 → `../todo.md` *Maintenance backlog*; R13 → `../todo_phase_2_*.md` (re-scoped) |
| `gap_review_2026-06-11.md` (V1–V11) | V1–V6 → `../todo_trials.md` (A0/E1 work breakdowns); V7/V8/V9 → `../todo_phase_4_*.md` *Open task specs*; V10 → executed into `../architecture.md` (§6 index correction, §8 routing note, §10 significance formula); V11 → `../todo.md` *Maintenance backlog* |
| `review_2026-06_architecture_and_plan.md`, `review_2026-06_architecture_plan.md`, `review_2026-06-11_post_phase4_review.md` | Findings records (point-in-time). Their surviving process lessons live in `../todo.md`: the deferred-items trigger table, the gate-first sequencing, and the conventions for executing agents |
| `review_2026-06-11_planned_architecture_assessment.md` (W1–W11) | Findings record (point-in-time; plan-vs-implementation assessment). Folded same day: W1/W2/W3 (composed-loop orchestrator, synthetic §8 end-to-end fixture, interim refutation-gate decision) → `../todo_phase_4_*.md` *Open task specs* + status block, with the R8 acceptance amendment (polarity-twin fixtures); W5/W6 → `../todo_phase_1_ingest.md` G1.23/G1.24 (temperature enforcement, context-span cache key); W7/W8/W11 → `../todo.md` *Maintenance backlog*; W9 → `../todo_trials.md` C3 scheduling amendment (before-Phase-5 backstop + edge-property query shapes); W10 → `../todo_trials.md` E2 de-scoping ladder; Phase 5 entry criteria → `../todo_phase_5_*.md` |
| `todo_ingest.md` (the D1–D5 + leads decision ledger) | Decisions executed into `../architecture.md` (§3.1 combiner + degraded-mode null rule; §10 nullable faithfulness + `agreement` + `Task.kind=inquiry`; §6.1 cache key + version-change policy; §2 level policy; §1 MinerU relicense; new §11.3 leads/moves/inquiries; §11.1 VoI-per-cost). Code deltas → `../todo_phase_1_ingest.md` G1.20–G1.22; D4 supersession → `../todo_phase_5_*.md`; leads runtime/UI → `../todo_phase_6_*.md` / `../todo_phase_7_*.md`; R8 amended (`unassessed_faithfulness`). **Corrections applied during the merge:** D3's "defer cross-doc extract-once" was overtaken — G1.7b shipped it with a context-inclusive key that resolves D3's objection; D2's "completes cheaply" required splitting verification out of the extraction cache key (G1.22); "sparse (BM25)" is FTS+RRF per §4; MinerU's relicense (AGPL → Apache-2.0-based w/ conditions) verified and folded into §1 |

If an archived file and a live file disagree, the live file wins;
`../architecture.md` remains the source of truth over both.
