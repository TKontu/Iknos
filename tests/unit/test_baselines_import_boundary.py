"""The V4 import boundary: the baselines package uses plumbing seams only, no iknos reasoning.

E1 is only valid if the baseline does **not** get the project's machinery (architecture.md §8).
This makes that structural: every ``iknos.*`` import in ``iknos.baselines`` must be on a small
allowlist of plumbing seams — the LLM client, the embedding substrate, the baseline-only chunk
table, and config. Any import of segmentation, propositions, the graph, candidate generation,
adjudication, QBAF, resolution, etc. fails this test (it is not on the allowlist), so a baseline
cannot quietly start leaning on iknos reasoning.
"""

from __future__ import annotations

import ast
from pathlib import Path

import iknos.baselines

BASELINES_DIR = Path(iknos.baselines.__file__).parent

# The plumbing seams a fair baseline may use (same endpoint + embedding model as the system),
# plus the package itself. Everything else under iknos.* is project reasoning and is forbidden.
ALLOWED_IKNOS_MODULES = {
    "iknos.core.llm",
    "iknos.core.embeddings",
    "iknos.db.orm",
    "iknos.config",
}
ALLOWED_PREFIX = "iknos.baselines"


def _iknos_imports(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return {m for m in modules if m == "iknos" or m.startswith("iknos.")}


def test_baselines_import_only_allowlisted_iknos_modules() -> None:
    offenders: dict[str, set[str]] = {}
    for path in BASELINES_DIR.rglob("*.py"):
        bad = {
            m
            for m in _iknos_imports(path.read_text(encoding="utf-8"))
            if m not in ALLOWED_IKNOS_MODULES and not m.startswith(ALLOWED_PREFIX)
        }
        if bad:
            offenders[str(path.relative_to(BASELINES_DIR))] = bad
    assert not offenders, (
        f"baselines import non-plumbing iknos modules (E1 must not use reasoning): {offenders}"
    )
