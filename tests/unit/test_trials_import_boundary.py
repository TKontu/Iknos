"""The V3 import boundary: ``iknos.trials`` is pure, never calls an LLM, needs no DATABASE_URL.

architecture.md §8/§13 require evaluation to be bias-controlled and **never LLM-as-judge**;
the V3 spec makes that a structural guarantee, not a convention: the trials harness must not
import ``iknos.core.llm``, and must import with ``DATABASE_URL`` unset. These tests enforce
both — an AST scan of every module in the package (so the rule holds even for a module not
exercised by the other tests) plus a clean-import check in a subprocess with the env stripped.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import iknos.trials

TRIALS_DIR = Path(iknos.trials.__file__).parent
FORBIDDEN_IMPORT = "iknos.core.llm"


def _module_dotted_name(path: Path) -> str:
    """The dotted module name of a file under ``iknos.trials`` (``__init__`` → the package)."""
    rel = path.relative_to(TRIALS_DIR).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["iknos.trials", *parts])


def _resolve_relative(
    module: str | None, level: int, current_module: str, is_pkg: bool
) -> str | None:
    """Resolve a relative ``from`` import to its absolute from-package name (``level > 0``).

    Without this a ``from ...core import llm`` would slip past as a ``level > 0`` import the scan
    ignored — the same bypass the V12 residual review flagged on the baselines boundary.
    """
    if level == 0:
        return module
    pkg = current_module if is_pkg else current_module.rpartition(".")[0]
    for _ in range(level - 1):
        pkg = pkg.rpartition(".")[0]
    return f"{pkg}.{module}" if module else pkg


def _imported_modules(source: str, *, current_module: str, is_pkg: bool) -> set[str]:
    """All module paths a source file imports (``import x`` and ``from x import y``, abs or rel)."""
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_relative(node.module, node.level, current_module, is_pkg)
            if resolved is not None:
                modules.add(resolved)
    return modules


def _modules_of(path: Path) -> set[str]:
    return _imported_modules(
        path.read_text(encoding="utf-8"),
        current_module=_module_dotted_name(path),
        is_pkg=path.name == "__init__.py",
    )


def test_no_trials_module_imports_core_llm() -> None:
    offenders: list[str] = []
    for path in TRIALS_DIR.rglob("*.py"):
        modules = _modules_of(path)
        if any(m == FORBIDDEN_IMPORT or m.startswith(FORBIDDEN_IMPORT + ".") for m in modules):
            offenders.append(str(path.relative_to(TRIALS_DIR)))
    assert not offenders, f"trials modules import {FORBIDDEN_IMPORT}: {offenders}"


def test_relative_import_bypass_is_detected() -> None:
    # Regression (V12): a relative `from ..core import llm` must resolve to the forbidden module.
    found = _imported_modules(
        "from ..core import llm\n", current_module="iknos.trials.metrics", is_pkg=False
    )
    assert "iknos.core" in found


def test_trials_imports_only_stdlib_and_self() -> None:
    # Stronger than the llm ban: the harness must stay dependency-free (no torch/numpy/DB),
    # so every non-stdlib import must be within iknos.trials itself.
    stdlib = set(sys.stdlib_module_names)
    offenders: dict[str, set[str]] = {}
    for path in TRIALS_DIR.rglob("*.py"):
        bad = {
            m
            for m in _modules_of(path)
            if m.split(".")[0] not in stdlib and not m.startswith("iknos.trials")
        }
        if bad:
            offenders[str(path.relative_to(TRIALS_DIR))] = bad
    assert not offenders, f"non-stdlib, non-self imports in trials: {offenders}"


def test_trials_imports_without_database_url() -> None:
    # A subprocess with DATABASE_URL stripped: importing the package must not touch config/DB.
    code = "import iknos.trials.metrics, iknos.trials.scoring, iknos.trials.report"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"PATH": "/usr/bin:/bin"},  # deliberately no DATABASE_URL, minimal env
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"import failed without DATABASE_URL:\n{result.stderr}"
