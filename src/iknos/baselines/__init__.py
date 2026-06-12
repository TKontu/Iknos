"""Baseline rigs for the E1 go/no-go (Trial A0 / V4–V5) — the cheap competitors to beat.

E1 (architecture.md §8, ``docs/todo_trials.md``) is the early go/no-go on the whole approach:
the iknos system must show **material lift over the strongest cheap baseline** on the
differentiator axes (contradiction/refuter handling, retraction, traceability, calibration).
A weak baseline biases E1 toward the system, so these rigs are built as *fair strong*
competitors — a competent team's RAG, not a strawman — sharing the system's LLM endpoint and
embedding model but **none** of its reasoning.

The boundary is enforced, not conventional (``tests/unit/test_baselines_import_boundary.py``):
a baseline may use the **plumbing** seams — ``iknos.core.llm`` (the LLM client),
``iknos.core.embeddings`` (the embedding substrate), ``iknos.db`` (sessions + the
baseline-only ``baseline_chunks`` table) and ``iknos.config`` — but must **never** import
iknos's segmentation, proposition, graph, candidate-generation, adjudication, or QBAF modules.
The whole point of E1 is that the baseline does *not* get the project's machinery.

All rungs emit the **same output contract** (:class:`~iknos.baselines.contract.BaselineAnswer`)
so the V3 harness scores the whole ladder identically:

* **V4 — plain RAG** (:mod:`iknos.baselines.rag`): fixed-size chunking, top-k cosine retrieval,
  one answer call. The retrieval-tuned baseline.
* **V5 — agentic / multi-hop RAG** (added on top of V4's retrieval): an LLM-driven
  search→answer loop.
"""
