"""MinerU HTTP parse client — the real Stage-0 ``Parser`` (§1, G1.0b).

``core/parse.py`` is the pure contract (``Parser`` protocol, ``ParseResult``,
``from_offsets``); this is the *I/O* layer that fills it from a network service, kept in
its own module exactly as ``core/llm.py`` is kept apart from the pure inference helpers, so
``parse.py`` stays unit-testable with no httpx/config import.

MinerU is AGPL-3.0, so it runs as a **separate hosted service** behind
``config.parser_base_url`` — the copyleft stops at the service edge (Docling/Marker are
drop-in alternates behind the same wire contract). This client deliberately speaks **our
own versioned wire schema**, not MinerU's native ``content_list.json``: a thin adapter on
the service side maps MinerU → this schema, so a MinerU upgrade (or a parser swap) that
reshapes the native output cannot silently break us — the coupling lives on the AGPL side.

Wire contract:

- **Request:** ``POST {base_url}/v1/parse`` with the raw document bytes as the body and
  ``Content-Type: <media_type>`` declaring what they are.
- **Response:** JSON ``{parser_name, parser_version, text, elements:[{kind, start, end,
  page?, bbox?, origin?, page_size?, unit?, source_quality?}]}`` — one reading-order text
  blob plus per-element ``[start, end)`` offsets into it and page geometry. ``parser_version``
  is the *service's* version (a MinerU upgrade changes it → ``parse_content_hash`` moves →
  downstream re-derives), never a constant here.

Robustness posture — **fail loud at the trust boundary** (the service is external):

- The response is validated by a pydantic model; a missing/mistyped field or an unknown
  ``kind``/``source_quality`` raises rather than silently coercing.
- ``ParseResult.from_offsets`` re-validates the geometry of the *tiling* (offsets in range,
  ordered, non-overlapping, no dropped text) and raises on any violation.
- Retries cover **transport errors and 5xx only**; 4xx (a bad request — our bug, not
  transient) and validation/tiling errors are not retried. Mirrors ``core/llm.py``.
"""

import httpx
from pydantic import BaseModel, ConfigDict
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from iknos.core.parse import (
    NullParser,
    OffsetSpec,
    ParseKind,
    Parser,
    ParseResult,
    SourceQuality,
)


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient failures only: transport/timeout/network errors and 5xx responses.

    A 4xx is a malformed request (our bug) and a pydantic/tiling error is a malformed
    *response* — neither is fixed by waiting, so both surface immediately. Mirrors the
    ``core/llm.py`` ``_RETRYABLE`` split (transport + InternalServerError, never 4xx/JSON).
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class _WireElement(BaseModel):
    """One element of the parse-service response (validated; unknown fields ignored).

    ``extra="ignore"`` keeps the client forward-compatible — the service may add fields
    without breaking us — while a *missing required* field or a bad enum value still raises.
    """

    model_config = ConfigDict(extra="ignore")

    kind: ParseKind
    start: int
    end: int
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    origin: str | None = None
    page_size: tuple[float, float] | None = None
    unit: str | None = None
    source_quality: SourceQuality | None = None


class _WireResponse(BaseModel):
    """The parse-service response envelope. ``parser_version`` is the service's own version."""

    model_config = ConfigDict(extra="ignore")

    parser_name: str
    parser_version: str
    text: str
    elements: list[_WireElement]


class MinerUParser:
    """``Parser`` over the MinerU HTTP service (cf. ``core/llm.py::LLMClient``)."""

    def __init__(self, base_url: str | None = None, *, timeout_s: float | None = None) -> None:
        # Only touch the config singleton (which requires DATABASE_URL) when a default is
        # actually needed — unit tests pass both and stay DB-free, like LLMClient.
        if base_url is None or timeout_s is None:
            from iknos.config import settings

            base_url = base_url if base_url is not None else settings.parser_base_url
            timeout_s = timeout_s if timeout_s is not None else settings.parser_timeout_s

        if not base_url:
            raise ValueError(
                "MinerUParser requires a non-empty base_url. Set PARSER_BASE_URL (and "
                "PARSER_KIND=mineru), or use the null parser by leaving PARSER_BASE_URL empty."
            )
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def parse(self, document_bytes: bytes, *, media_type: str) -> ParseResult:
        """POST the bytes to the parse service and build a validated :class:`ParseResult`."""
        response = await self._client.post(
            "/v1/parse",
            content=document_bytes,
            headers={"Content-Type": media_type},
        )
        response.raise_for_status()
        wire = _WireResponse.model_validate_json(response.content)
        specs = [
            OffsetSpec(
                kind=el.kind,
                start=el.start,
                end=el.end,
                page=el.page,
                bbox=el.bbox,
                origin=el.origin,
                page_size=el.page_size,
                unit=el.unit,
                source_quality=el.source_quality,
            )
            for el in wire.elements
        ]
        # from_offsets is the second, geometry-level validation gate (tiling/dropped text);
        # together with the pydantic model above, a subtly-wrong parse can never persist.
        return ParseResult.from_offsets(
            wire.text,
            specs,
            parser_name=wire.parser_name,
            parser_version=wire.parser_version,
        )

    async def aclose(self) -> None:
        """Close the underlying connection pool. Leaking it would exhaust sockets in a
        long-running ingest worker — the kind of slow-burn that only bites in production."""
        await self._client.aclose()

    async def __aenter__(self) -> "MinerUParser":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def make_parser(
    *,
    base_url: str | None = None,
    kind: str | None = None,
    timeout_s: float | None = None,
) -> Parser:
    """The single construction point for a Stage-0 parser (called by the API/CLI layer).

    An **empty base URL is the "no service" signal** regardless of kind → the identity
    (null) parser (plain-text ingest, a first-class mode). Otherwise ``kind`` selects the
    client; an unknown kind raises *here*, at startup, rather than at the first ingest.
    """
    # Only consult the config singleton (which requires DATABASE_URL) when a default is
    # actually needed — callers passing both stay DB-free, like LLMClient / MinerUParser.
    if base_url is None or kind is None:
        from iknos.config import settings

        base_url = settings.parser_base_url if base_url is None else base_url
        kind = settings.parser_kind if kind is None else kind

    if not base_url or kind == "null":
        return NullParser()
    if kind == "mineru":
        return MinerUParser(base_url=base_url, timeout_s=timeout_s)
    raise ValueError(f"Unknown PARSER_KIND {kind!r}; expected 'null' or 'mineru'.")
