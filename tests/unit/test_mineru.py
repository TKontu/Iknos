"""Unit tests for the MinerU HTTP parse client (G1.0b) — no network, no DB.

The client is driven against an ``httpx.MockTransport`` so the wire contract, the two
validation gates (pydantic envelope + ``from_offsets`` tiling), and the retry policy are
exercised without a live service. Mirrors ``test_llm.py``'s mocking discipline.
"""

import json

import httpx
import pydantic
import pytest

from iknos.core.mineru import MinerUParser, _is_retryable, make_parser
from iknos.core.parse import NullParser


def _ok_body(text: str = "Alpha\n\nBeta", elements: list[dict] | None = None) -> str:
    if elements is None:
        elements = [
            {
                "kind": "paragraph",
                "start": 0,
                "end": 5,
                "page": 1,
                "bbox": [10, 20, 100, 40],
                "origin": "top-left",
                "page_size": [612, 792],
                "unit": "pt",
                "source_quality": "digital",
            },
            {
                "kind": "paragraph",
                "start": 7,
                "end": 11,
                "page": 1,
                "bbox": [10, 60, 100, 80],
                "origin": "top-left",
                "page_size": [612, 792],
                "unit": "pt",
            },
        ]
    return json.dumps(
        {"parser_name": "mineru", "parser_version": "2.1.0", "text": text, "elements": elements}
    )


def _parser_with(handler) -> MinerUParser:  # type: ignore[no-untyped-def]
    p = MinerUParser(base_url="http://parser.invalid", timeout_s=5.0)
    p._client = httpx.AsyncClient(
        base_url="http://parser.invalid", transport=httpx.MockTransport(handler)
    )
    return p


# --- the retry predicate (pure, no sleeps) ---


def test_is_retryable_transport_and_5xx_only() -> None:
    req = httpx.Request("POST", "http://parser.invalid/v1/parse")
    assert _is_retryable(httpx.ConnectError("down", request=req)) is True
    assert _is_retryable(httpx.ReadTimeout("slow", request=req)) is True
    resp500 = httpx.Response(503, request=req)
    assert _is_retryable(httpx.HTTPStatusError("x", request=req, response=resp500)) is True
    resp400 = httpx.Response(400, request=req)
    assert _is_retryable(httpx.HTTPStatusError("x", request=req, response=resp400)) is False
    assert _is_retryable(ValueError("tiling")) is False


# --- request shape + happy path ---


@pytest.mark.asyncio
async def test_parse_posts_bytes_and_builds_result() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("Content-Type")
        seen["body"] = request.content
        return httpx.Response(200, text=_ok_body())

    parser = _parser_with(handler)
    result = await parser.parse(b"%PDF-1.7 ...", media_type="application/pdf")

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/parse"
    assert seen["content_type"] == "application/pdf"
    assert seen["body"] == b"%PDF-1.7 ..."

    assert result.parser_name == "mineru"
    assert result.parser_version == "2.1.0"  # the service's version, folded into the hash
    assert [e.text for e in result.elements] == ["Alpha", "Beta"]
    assert result.elements[0].has_region is True


# --- validation gates fail loud, no retry ---


@pytest.mark.asyncio
async def test_malformed_envelope_raises_validation_error_without_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(pydantic.ValidationError):
        await _parser_with(handler).parse(b"x", media_type="application/pdf")
    assert calls["n"] == 1  # a malformed response is not transient → not retried


@pytest.mark.asyncio
async def test_bad_tiling_surfaces_value_error_without_retry() -> None:
    calls = {"n": 0}

    # Envelope is well-formed but the offsets run past the text → from_offsets rejects.
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = _ok_body(
            text="short",
            elements=[{"kind": "paragraph", "start": 0, "end": 99}],
        )
        return httpx.Response(200, text=body)

    with pytest.raises(ValueError, match="out of bounds"):
        await _parser_with(handler).parse(b"x", media_type="application/pdf")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_4xx_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    with pytest.raises(httpx.HTTPStatusError):
        await _parser_with(handler).parse(b"x", media_type="application/pdf")
    assert calls["n"] == 1  # our bug, not transient → surfaces immediately


@pytest.mark.asyncio
async def test_5xx_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="warming up")
        return httpx.Response(200, text=_ok_body())

    result = await _parser_with(handler).parse(b"x", media_type="application/pdf")
    assert calls["n"] == 2
    assert [e.text for e in result.elements] == ["Alpha", "Beta"]


# --- make_parser factory ---


def test_make_parser_empty_url_is_null() -> None:
    assert isinstance(make_parser(base_url="", kind="mineru"), NullParser)  # empty url wins


def test_make_parser_kind_null_is_null() -> None:
    assert isinstance(make_parser(base_url="http://parser.invalid", kind="null"), NullParser)


def test_make_parser_kind_mineru() -> None:
    parser = make_parser(base_url="http://parser.invalid", kind="mineru", timeout_s=5.0)
    assert isinstance(parser, MinerUParser)


def test_make_parser_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="Unknown PARSER_KIND"):
        make_parser(base_url="http://parser.invalid", kind="bogus")


def test_mineru_requires_base_url() -> None:
    with pytest.raises(ValueError, match="non-empty base_url"):
        MinerUParser(base_url="", timeout_s=5.0)


# --- G1.18: the structured table payload survives the wire ---


def _table_body() -> str:
    # One TABLE element holding a 1x2 grid; cell offsets are element-relative (into the
    # element's own text "R1C1 R1C2"), per the wire contract.
    element = {
        "kind": "table",
        "start": 0,
        "end": 9,
        "page": 1,
        "bbox": [10, 20, 100, 40],
        "origin": "top-left",
        "page_size": [612, 792],
        "unit": "pt",
        "table": {
            "n_rows": 1,
            "n_cols": 2,
            "cells": [
                {"row": 0, "col": 0, "start": 0, "end": 4, "is_header": True},
                {"row": 0, "col": 1, "start": 5, "end": 9},
            ],
        },
    }
    return _ok_body(text="R1C1 R1C2", elements=[element])


@pytest.mark.asyncio
async def test_parse_builds_table_element_from_wire() -> None:
    parser = _parser_with(lambda request: httpx.Response(200, text=_table_body()))
    result = await parser.parse(b"%PDF-1.7 ...", media_type="application/pdf")

    [el] = result.elements
    assert el.kind.value == "table"
    assert el.table is not None
    assert (el.table.n_rows, el.table.n_cols) == (1, 2)
    # Element-relative offsets resolve against the element's own text.
    c0, c1 = el.table.cells
    assert c0.is_header is True
    assert el.text[c0.start : c0.end] == "R1C1"
    assert el.text[c1.start : c1.end] == "R1C2"


@pytest.mark.asyncio
async def test_malformed_table_grid_surfaces_value_error_without_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        bad = {
            "kind": "table",
            "start": 0,
            "end": 9,
            "table": {
                "n_rows": 1,
                "n_cols": 1,
                # Two cells claim the same grid position — Table.__post_init__ rejects it.
                "cells": [
                    {"row": 0, "col": 0, "start": 0, "end": 4},
                    {"row": 0, "col": 0, "start": 5, "end": 9},
                ],
            },
        }
        return httpx.Response(200, text=_ok_body(text="R1C1 R1C2", elements=[bad]))

    with pytest.raises(ValueError, match="already occupied"):
        await _parser_with(handler).parse(b"x", media_type="application/pdf")
    assert calls["n"] == 1  # a malformed table is our/the service's bug, not transient
