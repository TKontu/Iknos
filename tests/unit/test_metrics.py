"""Unit tests for the per-Action operational-metrics assembly (R12, observability floor).

Pins the one piece of policy in :mod:`iknos.provenance.metrics`: the "omit an absent key, never
zero it" rule for ``Action.metrics``. The token totals must be summed across the calls that
reported usage and omitted entirely when none did, so the §6.1 / Trials-A-C cost consumers can
tell "endpoint reported nothing" from a genuine zero.
"""

import time

from iknos.provenance.metrics import elapsed_ms, llm_metrics


def test_elapsed_ms_is_a_nonnegative_int() -> None:
    start = time.monotonic()
    out = elapsed_ms(start)
    assert isinstance(out, int)
    assert out >= 0


def test_llm_metrics_sums_token_usage_across_calls() -> None:
    metrics = llm_metrics(
        duration_ms=12,
        usages=[
            {"prompt_tokens": 10, "completion_tokens": 3},
            {"prompt_tokens": 5, "completion_tokens": 2},
        ],
        n_samples=2,
    )
    assert metrics == {
        "duration_ms": 12,
        "cache_hit": False,
        "n_samples": 2,
        "prompt_tokens": 15,
        "completion_tokens": 5,
    }


def test_llm_metrics_omits_token_keys_when_no_call_reported_usage() -> None:
    # All usage dicts empty (a vLLM config that returns no usage block) ⇒ token keys absent,
    # NOT written as 0 — the load-bearing R12 discipline.
    metrics = llm_metrics(duration_ms=4, usages=[{}, {}], n_samples=2)
    assert "prompt_tokens" not in metrics
    assert "completion_tokens" not in metrics
    assert metrics == {"duration_ms": 4, "cache_hit": False, "n_samples": 2}


def test_llm_metrics_sums_only_the_calls_that_reported() -> None:
    # A partial-usage batch (one sample reported, one did not) still sums what was reported,
    # never fabricating a zero for the silent call.
    metrics = llm_metrics(
        duration_ms=8,
        usages=[{"prompt_tokens": 9, "completion_tokens": 4}, {}],
        n_samples=2,
    )
    assert metrics["prompt_tokens"] == 9
    assert metrics["completion_tokens"] == 4


def test_llm_metrics_cache_replay_shape() -> None:
    # A G1.7b replay drew no sample and timed no LLM call: cache_hit=True, and duration/n_samples/
    # tokens are all omitted (absent, not zero).
    metrics = llm_metrics(usages=[], cache_hit=True)
    assert metrics == {"cache_hit": True}
