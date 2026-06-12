"""Unit tests for the out-of-process embedding backend (R10) — no network, no DB, no torch model.

The backend is driven against an ``httpx.MockTransport`` so the wire contract, the pydantic
validation gates (window/length/dimension mismatches, the G1.16 model-identity guard), and the
retry predicate are exercised without a live service or a loaded model. Mirrors
``test_mineru.py``'s mocking discipline. A small fixture also proves the reassembled
``DocumentContext`` is **interchangeable** with an in-process one: ``pool_span`` over the HTTP
windows returns the same vectors a directly-built context would.
"""

from __future__ import annotations

import json

import httpx
import pytest

from iknos.core.embeddings import (
    EMBEDDING_DIM,
    EmbeddingBackend,
    EmbeddingModelMismatchError,
    make_embedding_backend,
)
from iknos.core.embeddings_http import HTTPEmbeddingBackend, _is_retryable

MODEL = "BAAI/bge-m3"


def _vec(fill: float) -> list[float]:
    return [fill] * EMBEDDING_DIM


def _doc_body(
    *,
    model_version: str = MODEL,
    offsets: list[list[list[int]]] | None = None,
    embeddings: list[list[list[float]]] | None = None,
) -> str:
    if offsets is None:
        offsets = [[[0, 3], [4, 7]]]
    if embeddings is None:
        embeddings = [[_vec(0.1), _vec(0.2)]]
    return json.dumps(
        {
            "model_version": model_version,
            "windowing": {"overlap": 1024, "model_max_tokens": 8192, "window_token_size": 8189},
            "offsets": offsets,
            "embeddings": embeddings,
        }
    )


def _backend_with(handler) -> HTTPEmbeddingBackend:  # type: ignore[no-untyped-def]
    b = HTTPEmbeddingBackend(base_url="http://embed.invalid", model_name=MODEL, timeout_s=5.0)
    b._client = httpx.Client(
        base_url="http://embed.invalid", transport=httpx.MockTransport(handler)
    )
    return b


# --- the retry predicate (pure) ---------------------------------------------------------------


def test_is_retryable_transport_and_5xx_only() -> None:
    req = httpx.Request("POST", "http://embed.invalid/embed")
    assert _is_retryable(httpx.ConnectError("down", request=req)) is True
    assert _is_retryable(httpx.ReadTimeout("slow", request=req)) is True
    resp503 = httpx.Response(503, request=req)
    assert _is_retryable(httpx.HTTPStatusError("x", request=req, response=resp503)) is True
    resp400 = httpx.Response(400, request=req)
    assert _is_retryable(httpx.HTTPStatusError("x", request=req, response=resp400)) is False
    assert _is_retryable(ValueError("dim mismatch")) is False


# --- the factory seam -------------------------------------------------------------------------


def test_factory_returns_http_backend_for_a_url() -> None:
    b = make_embedding_backend(base_url="http://embed.invalid", model_name=MODEL, timeout_s=5.0)
    assert isinstance(b, HTTPEmbeddingBackend)
    assert isinstance(b, EmbeddingBackend)  # structurally satisfies the seam
    b.close()


def test_http_backend_requires_a_base_url() -> None:
    with pytest.raises(ValueError, match="non-empty base_url"):
        HTTPEmbeddingBackend(base_url="", model_name=MODEL, timeout_s=5.0)


# --- embed_passages (TEI /embed) --------------------------------------------------------------


def test_embed_passages_posts_inputs_and_returns_vectors() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[_vec(0.5), _vec(0.6)])

    backend = _backend_with(handler)
    out = backend.embed_passages(["alpha", "beta"])

    assert seen["path"] == "/embed"
    assert seen["body"] == {"inputs": ["alpha", "beta"]}
    assert out == [_vec(0.5), _vec(0.6)]


def test_embed_passages_empty_never_hits_the_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("empty embed_passages must not call the service")

    assert _backend_with(handler).embed_passages([]) == []


def test_embed_passages_rejects_wrong_count() -> None:
    backend = _backend_with(lambda r: httpx.Response(200, json=[_vec(0.5)]))  # 1 for 2 inputs
    with pytest.raises(ValueError, match="1 vectors for 2 inputs"):
        backend.embed_passages(["a", "b"])


def test_embed_passages_rejects_wrong_dimension() -> None:
    backend = _backend_with(lambda r: httpx.Response(200, json=[[0.1, 0.2, 0.3]]))
    with pytest.raises(ValueError, match="dimension 3"):
        backend.embed_passages(["a"])


# --- embed_document (windowed token+offset path) ----------------------------------------------


def test_embed_document_posts_request_and_builds_context() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=_doc_body())

    backend = _backend_with(handler)
    ctx = backend.embed_document("abc def")

    assert seen["path"] == "/embed_document"
    assert seen["body"]["text"] == "abc def"
    assert "window_tokens" in seen["body"] and "overlap_tokens" in seen["body"]
    # The windowing policy round-trips (it folds into the segmentation content hash).
    assert ctx.windowing_policy() == {
        "overlap": 1024,
        "model_max_tokens": 8192,
        "window_token_size": 8189,
    }


def test_embed_document_context_is_interchangeable_pool_span() -> None:
    # Token 0 spans chars [0,3) with vector 0.1·1; token 1 spans [4,7) with vector 0.2·1. A span
    # over [0,3) pools just token 0 → the normalized 0.1 vector (== a unit vector, here all-equal).
    backend = _backend_with(lambda r: httpx.Response(200, text=_doc_body()))
    ctx = backend.embed_document("abc def")
    pooled = ctx.pool_span(0, 3)
    assert pooled is not None
    # All dims equal and L2-normalized → each component is 1/sqrt(DIM).
    expected = 1.0 / (EMBEDDING_DIM**0.5)
    assert pooled[0] == pytest.approx(expected, rel=1e-5)
    # A whitespace-only span overlapping no token returns None (never a zero vector).
    assert ctx.pool_span(3, 4) is None


def test_embed_document_rejects_model_identity_mismatch() -> None:
    # G1.16: the service serving a different model than the backend declares must fail loud.
    body = _doc_body(model_version="some-other-model")
    backend = _backend_with(lambda r: httpx.Response(200, text=body))
    with pytest.raises(EmbeddingModelMismatchError, match="vector-space guard"):
        backend.embed_document("abc def")


def test_embed_document_rejects_window_length_mismatch() -> None:
    # 2 offsets but 1 embedding in the window → length mismatch, rejected at the boundary.
    body = _doc_body(offsets=[[[0, 3], [4, 7]]], embeddings=[[_vec(0.1)]])
    backend = _backend_with(lambda r: httpx.Response(200, text=body))
    with pytest.raises(Exception, match="length mismatch|offsets vs"):
        backend.embed_document("abc def")


def test_embed_document_rejects_wrong_embedding_dimension() -> None:
    body = _doc_body(offsets=[[[0, 3]]], embeddings=[[[0.1, 0.2, 0.3]]])
    backend = _backend_with(lambda r: httpx.Response(200, text=body))
    with pytest.raises(Exception, match="dimension 3"):
        backend.embed_document("abc def")


def test_malformed_response_is_not_retried() -> None:
    # A malformed response is not transient (the retry predicate excludes it), so it surfaces on
    # the first call — no exponential-backoff sleeping in the unit suite (mirrors test_mineru.py,
    # which likewise asserts the no-retry path rather than exercising real backoff).
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text='{"not": "a valid embed_document response"}')

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError, not retried
        _backend_with(handler).embed_document("abc def")
    assert calls["n"] == 1
