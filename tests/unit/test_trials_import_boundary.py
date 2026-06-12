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


def _imported_modules(source: str) -> set[str]:
    """All module paths a source file imports (both ``import x`` and ``from x import y``)."""
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


def test_no_trials_module_imports_core_llm() -> None:
    offenders: list[str] = []
    for path in TRIALS_DIR.rglob("*.py"):
        modules = _imported_modules(path.read_text(encoding="utf-8"))
        if any(m == FORBIDDEN_IMPORT or m.startswith(FORBIDDEN_IMPORT + ".") for m in modules):
            offenders.append(str(path.relative_to(TRIALS_DIR)))
    assert not offenders, f"trials modules import {FORBIDDEN_IMPORT}: {offenders}"


def test_trials_imports_only_stdlib_and_self() -> None:
    # Stronger than the llm ban: the harness must stay dependency-free (no torch/numpy/DB),
    # so every non-stdlib import must be within iknos.trials itself.
    stdlib = set(sys.stdlib_module_names)
    offenders: dict[str, set[str]] = {}
    for path in TRIALS_DIR.rglob("*.py"):
        bad = {
            m
            for m in _imported_modules(path.read_text(encoding="utf-8"))
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
