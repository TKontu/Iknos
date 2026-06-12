"""Out-of-process embedding backend (R10) — HTTP client behind the ``EmbeddingBackend`` seam.

``core/embeddings.py`` holds the in-process substrate, the ``EmbeddingBackend`` Protocol, and the
``make_embedding_backend`` factory; this is the I/O layer that fills the same contract from a
network service — kept in its own module exactly as ``core/mineru.py`` is kept apart from
``core/parse.py``, so the local (torch) path never imports httpx.

It speaks **our own versioned wire schema**, pydantic-validated and fail-loud at the trust
boundary (the service is external):

- ``embed_passages`` → ``POST {base_url}/embed`` with ``{"inputs": [...]}`` → ``[[float, …], …]``
  (TEI / text-embeddings-inference compatible: a bare JSON array, one pooled, L2-normalized vector
  per input). Empty input never hits the network.
- ``embed_document`` → ``POST {base_url}/embed_document`` with
  ``{text, window_tokens, overlap_tokens}`` → ``{model_version, windowing, offsets, embeddings}``.
  The per-token contextualized embeddings + char offsets the late-chunking pooling needs
  (:class:`~iknos.core.embeddings.DocumentContext`) cross the wire **per macro-window**; the server
  runs the same windowed forward pass ``EmbeddingSubstrate.embed_document`` does and returns the
  authoritative ``windowing`` policy, which folds into the *same* segmentation content hash — so an
  in-process and an out-of-process ingest of one document segment identically.

Robustness posture mirrors :class:`~iknos.core.mineru.MinerUParser`: retries cover **transport
errors and 5xx only** (a 4xx is our bug, a validation error is a malformed response — neither is
transient); a length/dimension mismatch or a **model-identity mismatch** (G1.16 — the service
serving a *different* model than this backend was built for) raises rather than coerce. The client
is **synchronous** (``httpx.Client``) so it is drop-in interchangeable with the in-process
substrate, which ``core/ingest.py`` calls inline.
"""

from __future__ import annotations

import httpx
import torch
from pydantic import BaseModel, ConfigDict, model_validator
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from iknos.core.embeddings import (
    EMBEDDING_DIM,
    MAX_MODEL_TOKENS,
    WINDOW_OVERLAP_TOKENS,
    DocumentContext,
    EmbeddingModelMismatchError,
)


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient failures only: transport/timeout/network errors and 5xx responses.

    A 4xx is a malformed request (our bug) and a pydantic/validation error is a malformed
    *response* — neither is fixed by waiting, so both surface immediately. Mirrors
    ``core/mineru.py::_is_retryable`` and the ``core/llm.py`` retry split.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


_RETRY = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)


class _WireWindowing(BaseModel):
    """The windowing policy the service applied — equals the in-process policy (content hash)."""

    model_config = ConfigDict(extra="ignore")

    overlap: int
    model_max_tokens: int
    window_token_size: int


class _WireDocResponse(BaseModel):
    """The ``/embed_document`` response: per-window content-token offsets + embeddings + policy.

    ``offsets[w]`` and ``embeddings[w]`` describe macro-window ``w``: ``offsets[w][i]`` is the
    ``(start, end)`` char span of content token ``i`` and ``embeddings[w][i]`` is its contextualized
    vector. ``protected_namespaces=()`` lets the spec field ``model_version`` past pydantic's
    ``model_`` guard.
    """

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    model_version: str
    windowing: _WireWindowing
    offsets: list[list[tuple[int, int]]]
    embeddings: list[list[list[float]]]

    @model_validator(mode="after")
    def _check_lengths(self) -> _WireDocResponse:
        if len(self.offsets) != len(self.embeddings):
            raise ValueError(
                f"window count mismatch: {len(self.offsets)} offset windows vs "
                f"{len(self.embeddings)} embedding windows"
            )
        for w, (offs, embs) in enumerate(zip(self.offsets, self.embeddings, strict=True)):
            if len(offs) != len(embs):
                raise ValueError(
                    f"window {w}: {len(offs)} offsets vs {len(embs)} embeddings (length mismatch)"
                )
            for v in embs:
                if len(v) != EMBEDDING_DIM:
                    raise ValueError(
                        f"window {w}: embedding dimension {len(v)} != expected {EMBEDDING_DIM}"
                    )
        return self


def _validate_passage_vectors(data: object, *, n: int) -> list[list[float]]:
    """Validate a TEI ``/embed`` response: ``n`` vectors, each of dimension ``EMBEDDING_DIM``."""
    if not isinstance(data, list):
        raise ValueError(f"embed response must be a JSON array, got {type(data).__name__}")
    if len(data) != n:
        raise ValueError(f"embed response returned {len(data)} vectors for {n} inputs")
    vectors: list[list[float]] = []
    for i, vec in enumerate(data):
        if not isinstance(vec, list) or len(vec) != EMBEDDING_DIM:
            got = len(vec) if isinstance(vec, list) else type(vec).__name__
            raise ValueError(f"embed vector {i}: dimension {got} != expected {EMBEDDING_DIM}")
        vectors.append([float(x) for x in vec])
    return vectors


class HTTPEmbeddingBackend:
    """``EmbeddingBackend`` over an HTTP embedding service (cf. ``core/mineru.py::MinerUParser``).

    Synchronous, so it slots in wherever ``EmbeddingSubstrate`` does. ``model_name`` is the model
    this backend was built for; the ``/embed_document`` response's ``model_version`` is checked
    against it (G1.16) so a mis-pointed service cannot silently mix vector spaces.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        model_name: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        # Only touch the config singleton (which requires DATABASE_URL) when a default is needed —
        # unit tests pass all three and stay DB-free, like MinerUParser.
        if base_url is None or model_name is None or timeout_s is None:
            from iknos.config import settings

            base_url = base_url if base_url is not None else settings.embeddings_base_url
            model_name = model_name if model_name is not None else settings.embedding_model
            timeout_s = timeout_s if timeout_s is not None else settings.embeddings_timeout_s

        if not base_url:
            raise ValueError(
                "HTTPEmbeddingBackend requires a non-empty base_url. Set EMBEDDINGS_BASE_URL, or "
                "leave it empty to use the in-process EmbeddingSubstrate (make_embedding_backend)."
            )
        self.model_name = model_name
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)

    @_RETRY
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embed standalone passages to one normalized vector each (TEI ``/embed``)."""
        if not texts:
            return []
        response = self._client.post("/embed", json={"inputs": texts})
        response.raise_for_status()
        return _validate_passage_vectors(response.json(), n=len(texts))

    @_RETRY
    def embed_document(self, text: str) -> DocumentContext:
        """Embed a document into a :class:`DocumentContext` of one or more macro-windows.

        Requests the windowed token+offset path; the server returns per-window contextualized token
        embeddings + offsets + the windowing policy, which are reassembled into the same
        ``DocumentContext`` the in-process substrate builds (interchangeable: ``pool_span`` behaves
        identically). ``window_tokens`` is advisory — the server reports the authoritative
        ``window_token_size`` (it knows its tokenizer's special-token budget) in ``windowing``.
        """
        response = self._client.post(
            "/embed_document",
            json={
                "text": text,
                "window_tokens": MAX_MODEL_TOKENS,
                "overlap_tokens": WINDOW_OVERLAP_TOKENS,
            },
        )
        response.raise_for_status()
        wire = _WireDocResponse.model_validate_json(response.content)
        if wire.model_version != self.model_name:
            raise EmbeddingModelMismatchError(
                f"embedding service served model {wire.model_version!r}, but this backend is built "
                f"for {self.model_name!r} (G1.16 vector-space guard — refusing to mix spaces)"
            )

        windows: list[tuple[torch.Tensor, list[tuple[int, int]]]] = []
        for offs, embs in zip(wire.offsets, wire.embeddings, strict=True):
            # (1, win_len, hidden) — the shape DocumentContext._Window expects.
            tensor = torch.tensor(embs, dtype=torch.float32).reshape(1, len(embs), EMBEDDING_DIM)
            windows.append((tensor, [(int(s), int(e)) for s, e in offs]))
        if not windows:
            # Empty / token-less document: one empty window, exactly as the in-process path does.
            windows = [(torch.zeros((1, 0, EMBEDDING_DIM)), [])]

        policy = {
            "overlap": wire.windowing.overlap,
            "model_max_tokens": wire.windowing.model_max_tokens,
            "window_token_size": wire.windowing.window_token_size,
        }
        return DocumentContext.from_windows(windows, windowing=policy)

    def close(self) -> None:
        """Close the connection pool (idempotent). Leaking it exhausts sockets in a long worker."""
        self._client.close()

    def __enter__(self) -> HTTPEmbeddingBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
