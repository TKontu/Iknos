import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from iknos.core.llm import LLMClient


def _client() -> LLMClient:
    return LLMClient(base_url="http://vllm.invalid/v1", model="test-model")


def _response(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_missing_model_raises():
    with pytest.raises(ValueError, match="LLM_MODEL"):
        LLMClient(base_url="http://vllm.invalid/v1", model="")


@pytest.mark.asyncio
async def test_guided_complete_parses_json_and_passes_guided_schema():
    client = _client()
    create = AsyncMock(return_value=_response('{"propositions": [{"text": "x"}]}'))
    client._client.chat.completions.create = create

    out = await client.guided_complete(
        [{"role": "user", "content": "hi"}],
        {"type": "object"},
        {"temperature": 0.0},
    )

    assert out == {"propositions": [{"text": "x"}]}
    kwargs = create.call_args.kwargs
    assert kwargs["extra_body"] == {"guided_json": {"type": "object"}}
    assert kwargs["model"] == "test-model"
    assert kwargs["temperature"] == 0.0


@pytest.mark.asyncio
async def test_retries_transient_error_then_succeeds():
    client = _client()
    req = httpx.Request("POST", "http://vllm.invalid/v1/chat/completions")
    create = AsyncMock(
        side_effect=[
            openai.APITimeoutError(request=req),  # retryable
            _response('{"propositions": []}'),
        ]
    )
    client._client.chat.completions.create = create

    out = await client.guided_complete([{"role": "user", "content": "hi"}], {"type": "object"})

    assert out == {"propositions": []}
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_does_not_retry_malformed_json():
    client = _client()
    create = AsyncMock(return_value=_response("this is not json"))
    client._client.chat.completions.create = create

    # JSONDecodeError is not a retryable transport error, so it surfaces immediately.
    with pytest.raises(json.JSONDecodeError):
        await client.guided_complete([{"role": "user", "content": "hi"}], {"type": "object"})

    assert create.await_count == 1
