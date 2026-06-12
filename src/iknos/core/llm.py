"""Async LLM client for the local vLLM OpenAI-compatible endpoint.

Structured output is guaranteed at decode time via vLLM's native guided decoding
(``extra_body={"guided_json": schema}``) — a grammar-level guarantee, not
reprompt-and-validate. Retries cover transient transport/5xx failures only; a
malformed response is not a normal failure mode under guided decoding, so JSON
and 4xx errors are deliberately not retried.
"""

import asyncio
import json
from typing import Any

import openai
from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Transient failures worth retrying: connection drops, timeouts, and 5xx from
# the inference server. Bad requests (4xx) and JSON errors are NOT retried.
_RETRYABLE: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)

# Default hard deadline for one guided_complete call including all retries (G1.17 R5). Mirrors
# config.Settings.llm_call_timeout_s; the default is used when a caller constructs an LLMClient
# without naming one (e.g. DB-free unit tests, which must not touch the config singleton).
DEFAULT_CALL_TIMEOUT_S = 180.0


class LLMClient:
    """Thin wrapper over AsyncOpenAI pointed at the vLLM endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        *,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> None:
        # Only consult the config singleton (which requires DATABASE_URL) when a
        # default is actually needed. Callers that pass both — e.g. unit tests —
        # stay fully DB-free; real callers omit them and run with a populated env.
        if base_url is None or model is None:
            from iknos.config import settings

            base_url = base_url if base_url is not None else settings.llm_base_url
            model = model if model is not None else settings.llm_model

        # Per-call hard deadline (G1.17 R5). Constant default keeps DB-free callers off the config
        # singleton; the live entrypoint passes settings.llm_call_timeout_s explicitly.
        self.call_timeout_s = call_timeout_s
        self.model = model
        if not self.model:
            raise ValueError(
                "No LLM model configured. Set the LLM_MODEL environment variable "
                "(the served vLLM model id) before running propositionization."
            )
        # vLLM ignores the API key but the client requires a non-empty value.
        self._client = AsyncOpenAI(base_url=base_url, api_key="EMPTY")

    async def guided_complete(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        sampling: dict[str, Any] | None = None,
        *,
        usage_out: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Run a chat completion constrained to ``json_schema``; return parsed JSON.

        The whole retrying call is wrapped in a hard ``asyncio.timeout`` (G1.17 R5): a hung
        endpoint that neither returns nor errors is cancelled at ``call_timeout_s`` and raises
        ``TimeoutError``, so it can never hold its concurrency permit (and starve the batch)
        indefinitely. The deadline sits *above* the tenacity backoff ceiling — it is a backstop
        for a pathological hang, not the normal per-attempt timeout (the OpenAI client owns that).

        ``usage_out`` (R12): an optional caller-owned dict that, when supplied, is populated in
        place with this call's ``{"prompt_tokens", "completion_tokens"}`` — the cost-discipline
        signal the §6.1 / Trials-A-C consumers read off ``actions.metrics``. It is left **empty**
        when the endpoint returned no usage block (some vLLM configs), so a caller records *absent*
        (key omitted) rather than a fabricated zero. A side channel, not a return value, so the
        bare-``dict`` return every existing caller (and test mock) relies on is unchanged; only the
        cost-instrumented paths (extract/verify) pass it.
        """
        async with asyncio.timeout(self.call_timeout_s):
            parsed, usage = await self._guided_complete_with_retries(
                messages, json_schema, sampling
            )
        if usage_out is not None:
            usage_out.update(usage)
        return parsed

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _guided_complete_with_retries(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        sampling: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """The retried inner call: transient transport/5xx failures are retried (see
        ``_RETRYABLE``); 4xx and JSON errors are not. Wrapped by :meth:`guided_complete`'s
        outer deadline. Returns ``(parsed, usage)``; ``usage`` is ``{}`` when the endpoint omits
        its usage block (some vLLM configs), so a caller never reads a fabricated zero."""
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            extra_body={"guided_json": json_schema},
            **(sampling or {}),
        )
        content = response.choices[0].message.content or ""
        parsed: dict[str, Any] = json.loads(content)
        usage: dict[str, int] = {}
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }
        return parsed, usage
