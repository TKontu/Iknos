"""Document parse front-end — the Stage 0 contract (§1, G1.0).

Real case documents are PDFs/scans (multi-column, tables, figures, OCR-only), not the
clean ``raw_text`` the integration tests feed today. The revised §1 adds a **Stage 0**
that turns a document into reading-order text plus per-element ``{page, bbox}`` visual
provenance, so a claim resolves to a *region on the original page image*, not just a
character offset. The default implementation is **MinerU**; it is AGPL-3.0, so it runs
as a separate hosted service behind ``config.parser_base_url`` (Docling/Marker are
alternates) — the copyleft stops at the service edge, exactly as the LLM/verifier do.

This module is the **contract** (swappable like ``core/llm.py``) plus the *identity*
parser (``NullParser``) for plain-text ingest. The real MinerU HTTP client, tables →
propositions and figures → vision-extract are later increments; this slice ships the
contract, the null parser, and the two pure functions ``ingest.py`` needs to wire
``Span.layout`` through the ``persist_spans(layouts=...)`` seam G1.9 already left open.

Deliberately a **pure leaf**: no DB, no torch, no LLM — exactly like ``core/cache.py``
and ``core/consistency.py``, so the whole contract is unit-testable in isolation.

Two structural decisions guard against silent corruption later:

- **Offsets are derived, never parser-supplied.** ``ParseResult.text`` is *defined* as
  the join of its elements' text; each element's char range is the cursor position that
  built that text (:meth:`ParseResult.char_ranges`). A parser that emits text via one
  path and offsets via another (every real one does) cannot drift them, because there
  is exactly one text and the offsets are how it was assembled. ``layouts_for_spans``
  maps segmentation spans onto elements through these derived ranges.
- **The persisted layout dict is opaque-but-versioned and multi-region.** A span
  routinely straddles a column or page break, so ``regions`` is always a list; each
  region carries ``origin`` + ``page_size`` + ``unit`` because a bbox is unrenderable
  against a re-rasterized page without them — and that geometry is *not recoverable*
  after the fact, so it is mandatory from day one.
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# Bump to deliberately invalidate prior parses (the element-join rule and element shape
# are part of this contract). Folded into the parse content hash, never an env knob —
# mirrors ``ingest.SEGMENT_SCHEMA_VERSION`` / ``proposition.EXTRACT_SCHEMA_VERSION``.
PARSE_SCHEMA_VERSION = 1

# The persisted ``Span.layout`` dict shape. Stored opaquely (the parser owns its shape)
# but versioned so the Phase-2 QA-overlay reader and the faithfulness reader can branch.
LAYOUT_SCHEMA_VERSION = 1

# Reading-order join between elements. Part of the contract (so PARSE_SCHEMA_VERSION):
# changing it re-assigns every char range, which must invalidate downstream parses.
_ELEMENT_JOIN = "\n\n"

_NULL_PARSER_NAME = "null"
_NULL_PARSER_VERSION = "1"


class ParseKind(StrEnum):
    """What a parsed element is. Tables/figures are *located* now, interpreted later."""

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    FIGURE = "figure"
    FORMULA = "formula"
    CAPTION = "caption"


class SourceQuality(StrEnum):
    """Parse-provenance quality of an element — a future faithfulness input (§3.1, G1.5).

    Carried into ``Span.layout`` now; *consumed* (scanned/handwritten → lower
    faithfulness → provisional → triage) in G1.5/G1.6. ``None`` means "not asserted".
    """

    DIGITAL = "digital"
    OCR = "ocr"
    HANDWRITTEN = "handwritten"


@dataclass(frozen=True)
class ParseElement:
    """One reading-order unit from the parser: its text, plus optional page geometry.

    ``text`` is the element's own text — char offsets are **not** carried here; they are
    assigned by :class:`ParseResult` during concatenation (see module docstring). Page
    geometry (``page`` + ``bbox`` + ``origin`` + ``page_size`` + ``unit``) is optional:
    the identity parser produces a text-only element with no region; a real parser fills
    it. ``has_region`` is the discriminator ``layouts_for_spans`` keys on.
    """

    kind: ParseKind
    text: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    origin: str | None = None  # bbox coordinate origin, e.g. "top-left"
    page_size: tuple[float, float] | None = None  # (width, height) in ``unit``
    unit: str | None = None  # "px" | "pt"
    source_quality: SourceQuality | None = None

    def __post_init__(self) -> None:
        # Empty text would create an ambiguous zero-width char range that no span can
        # unambiguously intersect; the null parser emits zero elements for empty input
        # rather than one empty element (see ``NullParser.parse_text``).
        if not self.text:
            raise ValueError("ParseElement.text must be non-empty")
        # A region is all-or-nothing: a bbox without a page (or vice versa) is unrenderable.
        if (self.page is None) != (self.bbox is None):
            raise ValueError("ParseElement page and bbox must be set together")

    @property
    def has_region(self) -> bool:
        """True iff this element carries page geometry (→ contributes a layout region)."""
        return self.page is not None and self.bbox is not None


@dataclass(frozen=True)
class ParseResult:
    """A parsed document: ordered elements + parser identity.

    ``text`` (the reading-order concatenation that becomes the segmentation input) and
    the per-element char ranges are **derived** from ``elements`` — there is no separate,
    drift-prone text field. ``parse_schema_version`` pins the join/element contract.
    """

    elements: tuple[ParseElement, ...]
    parser_name: str
    parser_version: str
    parse_schema_version: int = PARSE_SCHEMA_VERSION

    @property
    def text(self) -> str:
        """The reading-order text: elements joined by the contract's element separator."""
        return _ELEMENT_JOIN.join(e.text for e in self.elements)

    def char_ranges(self) -> list[tuple[int, int]]:
        """Per-element ``(start, end)`` char ranges into :attr:`text` (contract-assigned).

        By construction ``text[start:end] == element.text`` and the ranges tile ``text``
        monotonically with the element-join string in the gaps — offset drift is
        impossible because these *are* the cursor positions that built ``text``.
        """
        ranges: list[tuple[int, int]] = []
        cursor = 0
        for i, el in enumerate(self.elements):
            if i:
                cursor += len(_ELEMENT_JOIN)
            ranges.append((cursor, cursor + len(el.text)))
            cursor += len(el.text)
        return ranges

    @property
    def any_ocr(self) -> bool:
        """Whether any element is OCR/handwritten — a document-level faithfulness signal."""
        return any(
            e.source_quality in (SourceQuality.OCR, SourceQuality.HANDWRITTEN)
            for e in self.elements
        )


class Parser(Protocol):
    """The swappable parse contract (cf. ``core/llm.py::LLMClient``).

    Bytes in (not a bytes|path union): the AGPL constraint makes the real edge an HTTP
    service, so bytes is the honest interface — a path-based adapter reads then passes.
    """

    async def parse(self, document_bytes: bytes, *, media_type: str) -> ParseResult: ...


@dataclass(frozen=True)
class NullParser:
    """The *identity* parser: plain text in → that same text out, no page geometry.

    Plain-text ingest is a first-class supported mode, not a degraded one — so the null
    parser has a real, stable parse identity (``parser_name="null"`` + version), giving
    it a well-defined content hash like any other parser. It yields ``layout=None`` for
    every span while still exercising the full Stage-0 pipeline uniformly.
    """

    parser_name: str = field(default=_NULL_PARSER_NAME)
    parser_version: str = field(default=_NULL_PARSER_VERSION)

    def parse_text(self, text: str) -> ParseResult:
        """Wrap raw text as a single text-only element (or zero elements if empty)."""
        elements = (ParseElement(kind=ParseKind.PARAGRAPH, text=text),) if text else ()
        result = ParseResult(
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
        )
        # Identity invariant: the reading-order text must equal the input exactly, so the
        # offsets stored on spans (and document_content.raw_text) are unchanged vs. the
        # pre-Stage-0 plain-text path.
        assert result.text == text  # noqa: S101 — contract invariant, not input validation
        return result


def parse_content_hash(
    *,
    input_sha256: str,
    media_type: str,
    parser_name: str,
    parser_version: str,
    parse_schema_version: int,
) -> str:
    """SHA-256 over the parse **inputs** — the "parse once" / re-parse discriminator.

    Mirrors ``cache.extraction_content_hash`` / ``ingest.span_content_hash``: hash the
    inputs, never the derived ``ParseResult`` — OCR is non-deterministic, so hashing the
    output would defeat the cache and trip the re-parse guard on render drift. The bytes
    are represented by their digest (``input_sha256``) so the hash input stays bounded;
    ``media_type`` is in the key because the same bytes parsed as PDF vs image differ.

    Args:
        input_sha256: hex SHA-256 of the source document bytes (for the null/text path,
            of the UTF-8 raw text — it *is* the input).
        media_type: the declared media type, e.g. ``"application/pdf"`` / ``"text/plain"``.
        parser_name: the parser id (e.g. ``"mineru"`` / ``"null"``).
        parser_version: the parser's own version — a MinerU upgrade must invalidate.
        parse_schema_version: ``PARSE_SCHEMA_VERSION`` — the contract/join-rule version.
    """
    payload = {
        "input_sha256": input_sha256,
        "media_type": media_type,
        "parser_name": parser_name,
        "parser_version": parser_version,
        "parse_schema_version": parse_schema_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def layouts_for_spans(
    char_spans: list[tuple[int, int]], result: ParseResult
) -> list[dict[str, Any] | None]:
    """Map segmentation spans onto parse elements → one ``Span.layout`` dict (or None) each.

    Positionally aligned to ``char_spans`` (ready for ``persist_spans(layouts=...)``).
    For each span, every element whose derived char range has a **non-empty
    intersection** with the span contributes a region, in reading order; a span covering
    a column/page break therefore yields multiple regions. ``None`` when no overlapping
    element carries geometry — including the entire null-parser case (text-only
    elements), which reproduces the pre-Stage-0 layout-less behaviour exactly.

    The returned dict is the versioned, opaque-to-the-DB layout shape (see module
    docstring): ``{layout_schema_version, parser, regions:[{page, bbox, origin,
    page_size, unit, source_quality}, ...]}``.
    """
    ranges = result.char_ranges()
    out: list[dict[str, Any] | None] = []
    for span_start, span_end in char_spans:
        regions: list[dict[str, Any]] = []
        for el, (el_start, el_end) in zip(result.elements, ranges, strict=True):
            if not el.has_region:
                continue
            # Non-empty overlap of [span_start, span_end) and [el_start, el_end).
            if max(span_start, el_start) < min(span_end, el_end):
                regions.append(
                    {
                        "page": el.page,
                        "bbox": list(el.bbox) if el.bbox is not None else None,
                        "origin": el.origin,
                        "page_size": list(el.page_size) if el.page_size is not None else None,
                        "unit": el.unit,
                        "source_quality": el.source_quality.value if el.source_quality else None,
                    }
                )
        if regions:
            out.append(
                {
                    "layout_schema_version": LAYOUT_SCHEMA_VERSION,
                    "parser": result.parser_name,
                    "regions": regions,
                }
            )
        else:
            out.append(None)
    return out
