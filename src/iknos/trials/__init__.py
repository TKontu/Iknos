"""Trial evaluation harness (Trial A0 / V3) — metrics, bias-controlled scoring, reporting.

This package is the **evaluation instrument** the validation-gate trials (A1–A7, E1) score
themselves with, and the calibration measurement the gate needs (``docs/todo_trials.md``).
It is deliberately a thin, self-contained library:

* It is **pure** — :mod:`iknos.trials.metrics`, :mod:`iknos.trials.scoring` and
  :mod:`iknos.trials.report` import only the standard library. No torch, no numpy, no DB,
  no network — the package imports with ``DATABASE_URL`` unset.
* It **never calls an LLM.** Evaluation is bias-controlled (gold answers, controlled
  ordering), never LLM-as-judge (architecture.md §8, §13). An import-graph test
  (``tests/unit/test_trials_import_boundary.py``) enforces that nothing here imports
  ``iknos.core.llm``.
* It contains **no trial runners.** Each trial wires its own inputs when the V1 corpus and
  V2 labels exist; this package only supplies the order-invariant scoring schedule, the
  metric functions, and the markdown report renderer they share.
"""
