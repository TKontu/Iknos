"""Per-Action operational metrics assembly (R12, observability floor; §6.1).

The single place the "omit an absent key, never zero it" rule for ``Action.metrics`` lives, so
every instrumented operator builds its payload the same way. The §6.1 cost discipline and Trials
A/C read these numbers straight off ``actions.metrics``; a fabricated ``0`` token count would be
indistinguishable from a real zero, so when a source is genuinely absent (e.g. a vLLM endpoint
that returned no usage block, or a cache replay that drew no sample) the key is omitted entirely.

Two shapes, matching the two operator families:

- LLM-bearing Actions (``extract``/``verify``) → :func:`llm_metrics`:
  ``{duration_ms, n_samples, cache_hit, prompt_tokens, completion_tokens}`` (the token pair
  omitted when no call reported usage; ``n_samples`` omitted on a replay).
- text-processing Actions (``parse``/``segment``) carry plain count dicts assembled at their call
  sites (``core/ingest.py``) — no omission rule applies there since the counts are always known.

``duration_ms`` everywhere is a :func:`elapsed_ms` ``time.monotonic()`` delta.
"""

import time
from typing import Any


def elapsed_ms(start: float) -> int:
    """Whole milliseconds elapsed since a ``time.monotonic()`` reading (R12 ``duration_ms``).

    ``monotonic`` (never wall-clock) so an NTP/clock adjustment mid-call can never yield a negative
    or absurd duration; rounded to an int because sub-millisecond precision is noise next to an LLM
    round-trip or a batch of graph writes. Caller captures ``t0 = time.monotonic()`` and passes it.
    """
    return round((time.monotonic() - start) * 1000)


def llm_metrics(
    *,
    duration_ms: int | None = None,
    usages: list[dict[str, int]],
    n_samples: int | None = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    """Assemble an LLM-bearing Action's ``metrics`` payload (``extract``/``verify``, R12).

    ``usages`` is the list of per-call ``{"prompt_tokens", "completion_tokens"}`` dicts captured
    via :meth:`LLMClient.guided_complete`'s ``usage_out`` side channel (one per sample/proposition
    aggregated into this Action). Each is ``{}`` when that call's endpoint reported no usage. The
    token totals are **summed across the calls that reported usage** and omitted entirely — never
    written as ``0`` — when *none* did, so a consumer can tell "endpoint reported nothing" from a
    genuine zero. ``n_samples`` (the number of LLM calls aggregated) and ``duration_ms`` are each
    omitted when ``None`` — the cache-replay case, where no sample was drawn and no LLM call was
    timed (absent, not zero). ``cache_hit`` marks whether the Action reused a prior result instead
    of paying the LLM, the §6.1 amortization signal.
    """
    metrics: dict[str, Any] = {"cache_hit": cache_hit}
    if duration_ms is not None:
        metrics["duration_ms"] = duration_ms
    if n_samples is not None:
        metrics["n_samples"] = n_samples
    reported = [u for u in usages if u]
    if reported:
        metrics["prompt_tokens"] = sum(u.get("prompt_tokens", 0) for u in reported)
        metrics["completion_tokens"] = sum(u.get("completion_tokens", 0) for u in reported)
    return metrics
