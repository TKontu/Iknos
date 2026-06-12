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


def _module_dotted_name(path: Path) -> str:
    """The dotted module name of a file under ``iknos.baselines`` (``__init__`` → the package)."""
    rel = path.relative_to(BASELINES_DIR).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([ALLOWED_PREFIX, *parts])


def _resolve_relative(
    module: str | None, level: int, current_module: str, is_pkg: bool
) -> str | None:
    """Resolve a (possibly relative) ``from`` import to its absolute from-package dotted name.

    A relative import (``level > 0``) is resolved against the importing module's package, so
    ``from ..core import llm`` in ``iknos.baselines.rag`` resolves to ``iknos.core`` — which the
    allow/deny check then rejects. Without this, ``node.level > 0`` imports bypassed the boundary.
    """
    if level == 0:
        return module
    pkg = current_module if is_pkg else current_module.rpartition(".")[0]
    for _ in range(level - 1):
        pkg = pkg.rpartition(".")[0]
    return f"{pkg}.{module}" if module else pkg


def _iknos_imports(source: str, *, current_module: str, is_pkg: bool) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_relative(node.module, node.level, current_module, is_pkg)
            if resolved is not None:
                modules.add(resolved)
    return {m for m in modules if m == "iknos" or m.startswith("iknos.")}


def test_baselines_import_only_allowlisted_iknos_modules() -> None:
    offenders: dict[str, set[str]] = {}
    for path in BASELINES_DIR.rglob("*.py"):
        imports = _iknos_imports(
            path.read_text(encoding="utf-8"),
            current_module=_module_dotted_name(path),
            is_pkg=path.name == "__init__.py",
        )
        bad = {
            m
            for m in imports
            if m not in ALLOWED_IKNOS_MODULES and not m.startswith(ALLOWED_PREFIX)
        }
        if bad:
            offenders[str(path.relative_to(BASELINES_DIR))] = bad
    assert not offenders, (
        f"baselines import non-plumbing iknos modules (E1 must not use reasoning): {offenders}"
    )


def test_relative_import_bypass_is_detected() -> None:
    # Regression (V12): a `from ..core import llm` must resolve to a forbidden absolute module,
    # not slip through as a `level > 0` import the guard used to ignore.
    found = _iknos_imports(
        "from ..core import llm\n", current_module="iknos.baselines.rag", is_pkg=False
    )
    assert "iknos.core" in found
    assert "iknos.core" not in ALLOWED_IKNOS_MODULES  # → flagged as an offender
