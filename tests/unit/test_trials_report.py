"""The markdown report renderer (``iknos.trials.report``)."""

from __future__ import annotations

from iknos.trials.report import comparison_table, format_value, metrics_table


def test_format_value_float_precision() -> None:
    assert format_value(0.123456) == "0.1235"
    assert format_value(0.123456, float_precision=2) == "0.12"


def test_format_value_nan_is_dash() -> None:
    assert format_value(float("nan")) == "—"


def test_format_value_inf() -> None:
    assert format_value(float("inf")) == "∞"
    assert format_value(float("-inf")) == "-∞"


def test_format_value_passthrough_types() -> None:
    assert format_value(5) == "5"
    assert format_value("n/a") == "n/a"
    assert format_value(True) == "True"


def test_metrics_table_renders_rows_in_order() -> None:
    table = metrics_table({"refuter_recall": 0.8, "ece": 0.05, "n": 12})
    assert table == (
        "| Metric | Value |\n"
        "| --- | --- |\n"
        "| refuter_recall | 0.8000 |\n"
        "| ece | 0.0500 |\n"
        "| n | 12 |"
    )


def test_metrics_table_custom_headers() -> None:
    table = metrics_table({"k": 1.0}, name_header="Axis", value_header="Score")
    assert table.splitlines()[0] == "| Axis | Score |"


def test_comparison_table_union_columns_and_missing() -> None:
    table = comparison_table(
        {
            "plain_rag": {"refuter_recall": 0.2, "ece": 0.3},
            "system": {"refuter_recall": 0.9, "retraction": 1.0},
        }
    )
    lines = table.splitlines()
    # Columns are the first-seen union: refuter_recall, ece, retraction.
    assert lines[0] == "| System | refuter_recall | ece | retraction |"
    assert lines[2] == "| plain_rag | 0.2000 | 0.3000 | — |"
    assert lines[3] == "| system | 0.9000 | — | 1.0000 |"
