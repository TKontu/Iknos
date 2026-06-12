"""Offline tests for the gate-corpus dry-run runner (`scripts/run_gate_ingest.py`).

These are the regression guards for the two defects a 2026-06-12 review found in the runner
(FIX-1): the `--plan` offline path (no DB, deterministic ids), and R12-metrics detection against
the **real** ORM — the guard that wrongly looked for top-level `duration_ms`/`cost` columns and so
reported "R12 not merged" forever, when R12 shipped `metrics` as a JSONB column.

`scripts/` is not on the default `pythonpath` (only `src/` is), so the repo root is inserted here;
the runner module is import-light (config/DB are imported lazily inside functions), so importing it
and exercising `--plan` touches no database.
"""

import argparse
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.run_gate_ingest import (  # noqa: E402  (path insertion must precede the import)
    _actions_have_metrics,
    load_documents,
    run,
)

_CORPUS = "tests/fixtures/gate_corpus"


@pytest.mark.asyncio
async def test_plan_mode_lists_every_doc_with_deterministic_ids_and_touches_no_db() -> None:
    # --plan returns before any enqueue/drain/sanity-read and imports no config singleton, so it
    # runs with no DATABASE_URL. It must list every manifest document with its uuid5 id.
    docs = load_documents(Path(_CORPUS))
    args = argparse.Namespace(corpus=_CORPUS, box="gate-corpus", out=None, plan=True)

    report = await run(args)

    assert report.startswith("# Gate-corpus dry-run ingest — PLAN (no DB touched)")
    # Every document and its deterministic id appears exactly once (the production enqueue uses the
    # same uuid5(DOC_NAMESPACE, key), so the plan is a faithful preview of what would be deferred).
    assert docs, "the corpus manifest must list documents"
    for d in docs:
        assert d.key in report
        assert str(d.document_id) in report
    # The >8,192-token multi-window document the d08 check depends on is in scope.
    assert any(d.key == "d08" for d in docs)
    # One table row per document (header + separator + N rows), so nothing is silently dropped.
    assert report.count("\n|") == len(docs) + 2


def test_actions_have_metrics_detects_the_r12_metrics_jsonb_column() -> None:
    # R12 (#103) shipped `actions.metrics` as a JSONB column whose KEYS are duration_ms / tokens /
    # span counts — not as top-level duration_ms/cost columns. The pre-fix guard looked for those
    # top-level names and returned False forever; this pins detection of the real column.
    assert _actions_have_metrics() is True

    from iknos.db.orm import Action

    cols = set(Action.__table__.columns.keys())
    assert "metrics" in cols
    # The old, wrong signal must NOT be what we depend on (it was never written as a column).
    assert not (cols & {"duration_ms", "cost", "duration", "latency_ms"})
