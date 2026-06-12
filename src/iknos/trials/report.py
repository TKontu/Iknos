"""Render a metrics dict to a markdown table (Trial A0 / V3).

The trials produce dicts of metric → value; the gate write-up and the E1 baseline-ladder
comparison want those as readable markdown tables. This module is that renderer and nothing
more — pure formatting, no plotting dependency (the numbers are the deliverable; a reader or a
downstream tool draws charts if it wants them). Pure standard library.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

# A metric value is a number or a pre-formatted string (e.g. a count, a label, "n/a").
Value = float | int | str


def format_value(value: Value, *, float_precision: int = 4) -> str:
    """Format one metric value for a table cell.

    Floats render to ``float_precision`` decimals; ``nan`` (a deliberately "undefined" metric,
    e.g. Spearman ρ of a constant) renders as ``—`` so it is not misread as a real 0; ``inf``
    renders with its sign. Ints and strings pass through unchanged.
    """
    if isinstance(value, bool):  # bool is an int subclass; keep True/False readable
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "—"
        if math.isinf(value):
            return "∞" if value > 0 else "-∞"
        return f"{value:.{float_precision}f}"
    return str(value)


def metrics_table(
    metrics: Mapping[str, Value],
    *,
    name_header: str = "Metric",
    value_header: str = "Value",
    float_precision: int = 4,
) -> str:
    """Render a ``{metric_name: value}`` mapping as a two-column markdown table.

    Insertion order is preserved (so the caller controls row order). Returns the table as a
    string with no trailing newline.
    """
    rows = [f"| {name_header} | {value_header} |", "| --- | --- |"]
    for name, value in metrics.items():
        rows.append(f"| {name} | {format_value(value, float_precision=float_precision)} |")
    return "\n".join(rows)


def comparison_table(
    rows: Mapping[str, Mapping[str, Value]],
    *,
    row_header: str = "System",
    float_precision: int = 4,
) -> str:
    """Render a ``{row_label: {metric: value}}`` mapping as a system × metric markdown table.

    The natural shape for the E1 baseline ladder (plain RAG / agentic RAG / expert+search / the
    system, each scored on the same differentiator axes) and the E2 ablation arms. The column
    set is the **union** of every row's metric keys, in first-seen order; a value missing for a
    given row renders as ``—``. Insertion order of ``rows`` is preserved.
    """
    columns: list[str] = []
    for metrics in rows.values():
        for col in metrics:
            if col not in columns:
                columns.append(col)
    header = "| " + " | ".join([row_header, *columns]) + " |"
    sep = "| " + " | ".join(["---"] * (len(columns) + 1)) + " |"
    lines = [header, sep]
    for label, metrics in rows.items():
        cells = [label]
        for col in columns:
            if col in metrics:
                cells.append(format_value(metrics[col], float_precision=float_precision))
            else:
                cells.append("—")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
