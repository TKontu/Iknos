"""Async LLM client for the local vLLM OpenAI-compatible endpoint.

Structured output is guaranteed at decode time via vLLM's native guided decoding
(``extra_body={"guided_json": schema}``) — a grammar-level guarantee, not
reprompt-and-validate. Retries cover transient transport/5xx failures only; a
malformed response is not a normal failure mode under guided decoding, so JSON
and 4xx errors are deliberately not retried.
"""

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


class LLMClient:
    """Thin wrapper over AsyncOpenAI pointed at the vLLM endpoint."""

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        # Only consult the config singleton (which requires DATABASE_URL) when a
        # default is actually needed. Callers that pass both — e.g. unit tests —
        # stay fully DB-free; real callers omit them and run with a populated env.
        if base_url is None or model is None:
            from iknos.config import settings

            base_url = base_url if base_url is not None else settings.llm_base_url
            model = model if model is not None else settings.llm_model

        self.model = model
        if not self.model:
            raise ValueError(
                "No LLM model configured. Set the LLM_MODEL environment variable "
                "(the served vLLM model id) before running propositionization."
            )
        # vLLM ignores the API key but the client requires a non-empty value.
        self._client = AsyncOpenAI(base_url=base_url, api_key="EMPTY")

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def guided_complete(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        sampling: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a chat completion constrained to ``json_schema``; return parsed JSON."""
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            extra_body={"guided_json": json_schema},
            **(sampling or {}),
        )
        content = response.choices[0].message.content or ""
        parsed: dict[str, Any] = json.loads(content)
        return parsed
