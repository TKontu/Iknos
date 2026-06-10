"""Unit tests for the Stage 0 parse contract (G1.0) — pure, no DB/LLM/torch.

Three properties carry the slice's correctness: (1) ``ParseResult.text`` and the
per-element char ranges are *derived* and tile the text by construction (offset drift
is impossible); (2) ``parse_content_hash`` moves iff a parse input changes (the
"parse once" / re-parse discriminator, exactly like ``test_cache.py`` does for
extraction); (3) ``layouts_for_spans`` maps spans onto the right element regions,
including the multi-region (column/page-break) and no-region (null parser) cases.
"""

import hashlib

import pytest

from iknos.core.parse import (
    LAYOUT_SCHEMA_VERSION,
    NullParser,
    OffsetSpec,
    ParseElement,
    ParseKind,
    ParseResult,
    SourceQuality,
    layouts_for_spans,
    parse_content_hash,
)


def _located(
    text: str, *, page: int, bbox: tuple[float, float, float, float], **kw: object
) -> ParseElement:
    """A real-parser element with page geometry (the layout-bearing case)."""
    return ParseElement(
        kind=ParseKind.PARAGRAPH,
        text=text,
        page=page,
        bbox=bbox,
        origin="top-left",
        page_size=(612.0, 792.0),
        unit="pt",
        **kw,  # type: ignore[arg-type]
    )


# --- ParseElement construction invariants ---


def test_empty_element_text_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ParseElement(kind=ParseKind.PARAGRAPH, text="")


def test_page_and_bbox_must_be_set_together() -> None:
    with pytest.raises(ValueError, match="together"):
        ParseElement(kind=ParseKind.PARAGRAPH, text="x", page=1)
    with pytest.raises(ValueError, match="together"):
        ParseElement(kind=ParseKind.PARAGRAPH, text="x", bbox=(0, 0, 1, 1))


def test_text_only_element_has_no_region() -> None:
    assert ParseElement(kind=ParseKind.PARAGRAPH, text="x").has_region is False
    assert _located("x", page=2, bbox=(0, 0, 1, 1)).has_region is True


# --- derived text + char ranges (drift impossible by construction) ---


def test_text_is_join_of_elements() -> None:
    r = ParseResult(
        elements=(
            ParseElement(kind=ParseKind.HEADING, text="Title"),
            ParseElement(kind=ParseKind.PARAGRAPH, text="Body."),
        ),
        parser_name="x",
        parser_version="1",
    )
    assert r.text == "Title\n\nBody."


def test_char_ranges_tile_text_and_recover_each_element() -> None:
    elems = (
        ParseElement(kind=ParseKind.PARAGRAPH, text="Alpha one."),
        ParseElement(kind=ParseKind.PARAGRAPH, text="Beta two."),
        ParseElement(kind=ParseKind.PARAGRAPH, text="Gamma three."),
    )
    r = ParseResult(elements=elems, parser_name="x", parser_version="1")
    ranges = r.char_ranges()
    assert len(ranges) == 3
    prev_end = 0
    for el, (start, end) in zip(elems, ranges, strict=True):
        # Each range recovers exactly that element's text...
        assert r.text[start:end] == el.text
        # ...and ranges are monotonic (gaps are exactly the element join).
        assert start >= prev_end
        prev_end = end


def test_any_ocr_signal() -> None:
    digital = ParseResult(
        elements=(_located("a", page=1, bbox=(0, 0, 1, 1), source_quality=SourceQuality.DIGITAL),),
        parser_name="x",
        parser_version="1",
    )
    scanned = ParseResult(
        elements=(_located("a", page=1, bbox=(0, 0, 1, 1), source_quality=SourceQuality.OCR),),
        parser_name="x",
        parser_version="1",
    )
    assert digital.any_ocr is False
    assert scanned.any_ocr is True


# --- NullParser identity ---


def test_null_parser_is_identity() -> None:
    text = "First sentence. Second sentence.\n\nA new paragraph here."
    result = NullParser().parse_text(text)
    assert result.text == text  # the load-bearing identity invariant
    assert result.parser_name == "null"
    assert len(result.elements) == 1
    assert result.elements[0].has_region is False


def test_null_parser_empty_text_has_no_elements() -> None:
    result = NullParser().parse_text("")
    assert result.text == ""
    assert result.elements == ()


def test_null_parser_layouts_are_all_none() -> None:
    result = NullParser().parse_text("Claim one. Claim two.")
    layouts = layouts_for_spans([(0, 10), (11, 21)], result)
    assert layouts == [None, None]


# --- parse_content_hash: determinism + per-dimension sensitivity ---

_BASE_HASH = {
    "input_sha256": hashlib.sha256(b"doc bytes").hexdigest(),
    "media_type": "application/pdf",
    "parser_name": "mineru",
    "parser_version": "2.0.1",
    "parse_schema_version": 1,
}


def _h(**overrides: object) -> str:
    return parse_content_hash(**{**_BASE_HASH, **overrides})  # type: ignore[arg-type]


def test_parse_hash_is_sha256_hex() -> None:
    h = _h()
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_parse_hash_is_deterministic() -> None:
    assert _h() == _h()


def test_input_bytes_change_hash() -> None:
    assert _h(input_sha256=hashlib.sha256(b"other bytes").hexdigest()) != _h()


def test_media_type_changes_hash() -> None:
    # Same bytes parsed as PDF vs image is a different parse.
    assert _h(media_type="image/png") != _h()


def test_parser_name_changes_hash() -> None:
    assert _h(parser_name="docling") != _h()


def test_parser_version_changes_hash() -> None:
    # The production bug class: a MinerU upgrade must invalidate cached parses.
    assert _h(parser_version="2.1.0") != _h()


def test_parse_schema_version_changes_hash() -> None:
    assert _h(parse_schema_version=2) != _h()


# --- layouts_for_spans: overlap semantics ---


def _multi_page_result() -> ParseResult:
    # Two located elements; with the "\n\n" join their char ranges are:
    #   "Alpha block." -> [0, 12)   then join [12, 14)   then "Beta block." -> [14, 25)
    return ParseResult(
        elements=(
            _located("Alpha block.", page=1, bbox=(10, 20, 100, 40)),
            _located("Beta block.", page=2, bbox=(10, 60, 100, 80)),
        ),
        parser_name="mineru",
        parser_version="2.0.1",
    )


def test_single_element_overlap_one_region() -> None:
    r = _multi_page_result()
    [layout] = layouts_for_spans([(0, 12)], r)
    assert layout is not None
    assert layout["layout_schema_version"] == LAYOUT_SCHEMA_VERSION
    assert layout["parser"] == "mineru"
    assert len(layout["regions"]) == 1
    region = layout["regions"][0]
    assert region["page"] == 1
    assert region["bbox"] == [10, 20, 100, 40]
    assert region["origin"] == "top-left"
    assert region["page_size"] == [612.0, 792.0]
    assert region["unit"] == "pt"


def test_span_straddling_two_elements_yields_two_regions() -> None:
    r = _multi_page_result()
    # A span from inside element 0 through into element 1 (crosses the join + page break).
    [layout] = layouts_for_spans([(5, 20)], r)
    assert layout is not None
    pages = [region["page"] for region in layout["regions"]]
    assert pages == [1, 2]  # multi-region, in reading order


def test_span_in_join_gap_has_no_region() -> None:
    r = _multi_page_result()
    # [12, 14) is exactly the element-join gap — overlaps no element.
    assert layouts_for_spans([(12, 14)], r) == [None]


def test_partial_overlap_counts() -> None:
    r = _multi_page_result()
    # A span ending one char into element 0 still overlaps it.
    [layout] = layouts_for_spans([(0, 1)], r)
    assert layout is not None and layout["regions"][0]["page"] == 1


def test_source_quality_carried_into_region() -> None:
    r = ParseResult(
        elements=(
            _located("Scanned text.", page=1, bbox=(0, 0, 1, 1), source_quality=SourceQuality.OCR),
        ),
        parser_name="mineru",
        parser_version="2.0.1",
    )
    [layout] = layouts_for_spans([(0, 13)], r)
    assert layout is not None
    assert layout["regions"][0]["source_quality"] == "ocr"


# --- ParseElement geometry-frame invariant (G1.0b: a bbox needs its full frame) ---


def test_bbox_requires_full_coordinate_frame() -> None:
    # A bbox is unrenderable without origin/page_size/unit, and that frame is unrecoverable
    # later — so a half-specified region is refused at construction, not persisted.
    for missing in ("origin", "page_size", "unit"):
        frame: dict[str, object] = {
            "origin": "top-left",
            "page_size": (612.0, 792.0),
            "unit": "pt",
        }
        del frame[missing]
        with pytest.raises(ValueError, match="coordinate frame"):
            ParseElement(kind=ParseKind.PARAGRAPH, text="x", page=1, bbox=(0, 0, 1, 1), **frame)  # type: ignore[arg-type]


def test_full_frame_bbox_is_accepted() -> None:
    el = _located("x", page=1, bbox=(0, 0, 1, 1))
    assert el.has_region is True


# --- NullParser as a Parser (bytes-in degradation path) ---


@pytest.mark.asyncio
async def test_null_parser_parse_bytes_decodes_utf8() -> None:
    result = await NullParser().parse(b"Plain bytes in.", media_type="text/plain")
    assert result.text == "Plain bytes in."
    assert result.parser_name == "null"
    assert result.elements[0].has_region is False


@pytest.mark.asyncio
async def test_null_parser_parse_rejects_non_utf8_loudly() -> None:
    # Feeding real document bytes (a PDF) to the null parser must fail, not make mojibake.
    with pytest.raises(UnicodeDecodeError):
        await NullParser().parse(b"\xff\xfe\x00\x01PDF", media_type="application/pdf")


# --- ParseResult.from_offsets: the real-parser entry point (slice + fail-loud tiling) ---


def _spec(start: int, end: int, **kw: object) -> OffsetSpec:
    return OffsetSpec(kind=ParseKind.PARAGRAPH, start=start, end=end, **kw)  # type: ignore[arg-type]


def _from_offsets(text: str, specs: list[OffsetSpec]) -> ParseResult:
    return ParseResult.from_offsets(text, specs, parser_name="mineru", parser_version="2.1.0")


def test_from_offsets_slices_element_text_from_blob() -> None:
    blob = "Heading here\n\nFirst paragraph body."
    r = _from_offsets(blob, [_spec(0, 12), _spec(14, 35)])
    # Element text is sliced from the blob — never a separately-supplied string.
    assert [e.text for e in r.elements] == ["Heading here", "First paragraph body."]
    # And the derived result behaves like any other: text[start:end] recovers each element.
    for el, (s, e) in zip(r.elements, r.char_ranges(), strict=True):
        assert r.text[s:e] == el.text


def test_from_offsets_carries_geometry_into_elements() -> None:
    blob = "Located block."
    r = _from_offsets(
        blob,
        [
            OffsetSpec(
                kind=ParseKind.PARAGRAPH,
                start=0,
                end=14,
                page=3,
                bbox=(10, 20, 100, 40),
                origin="top-left",
                page_size=(612.0, 792.0),
                unit="pt",
                source_quality=SourceQuality.OCR,
            )
        ],
    )
    [layout] = layouts_for_spans([(0, 14)], r)
    assert layout is not None
    assert layout["regions"][0]["page"] == 3
    assert layout["regions"][0]["source_quality"] == "ocr"


def test_from_offsets_empty_specs_is_empty_result() -> None:
    r = _from_offsets("", [])
    assert r.text == "" and r.elements == ()


def test_from_offsets_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        _from_offsets("short", [_spec(0, 99)])


def test_from_offsets_rejects_empty_element() -> None:
    with pytest.raises(ValueError, match="out of bounds or empty"):
        _from_offsets("abc", [_spec(1, 1)])


def test_from_offsets_rejects_overlap() -> None:
    blob = "abcdefghij"
    with pytest.raises(ValueError, match="reading order and non-overlapping"):
        _from_offsets(blob, [_spec(0, 6), _spec(4, 10)])


def test_from_offsets_rejects_out_of_order() -> None:
    # Whitespace prefix so the first (positionally-later) element clears the dropped-text
    # guard; the second element going backwards is what must trip the ordering check.
    blob = "     Beta"
    with pytest.raises(ValueError, match="reading order and non-overlapping"):
        _from_offsets(blob, [_spec(5, 9), _spec(0, 4)])


def test_from_offsets_rejects_dropped_text_in_gap() -> None:
    # Real characters between two elements would silently vanish from reading order.
    blob = "Alpha DROPPED Beta"
    with pytest.raises(ValueError, match="dropped"):
        _from_offsets(blob, [_spec(0, 5), _spec(14, 18)])


def test_from_offsets_rejects_trailing_dropped_text() -> None:
    blob = "Alpha trailing-junk"
    with pytest.raises(ValueError, match="trailing remainder"):
        _from_offsets(blob, [_spec(0, 5)])


def test_from_offsets_allows_whitespace_gaps() -> None:
    # Whitespace between/around elements is fine (it carries no claim) and is normalized away
    # by the element-join in the derived text.
    blob = "  Alpha   Beta  "
    r = _from_offsets(blob, [_spec(2, 7), _spec(10, 14)])
    assert [e.text for e in r.elements] == ["Alpha", "Beta"]
    assert r.text == "Alpha\n\nBeta"
