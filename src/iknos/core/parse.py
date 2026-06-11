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
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# Bump to deliberately invalidate prior parses (the element-join rule and element shape
# are part of this contract). Folded into the parse content hash, never an env knob —
# mirrors ``ingest.SEGMENT_SCHEMA_VERSION`` / ``proposition.EXTRACT_SCHEMA_VERSION``.
PARSE_SCHEMA_VERSION = 1

# The persisted ``Span.layout`` dict shape. Stored opaquely (the parser owns its shape)
# but versioned so the Phase-2 QA-overlay reader and the faithfulness reader can branch.
# v2 (G1.18): a region may carry a ``table`` payload (structured cell grid with
# document-absolute cell offsets); a region entry may also be geometry-less (all-None
# page/bbox) when it exists only to carry a table whose element lacked page geometry.
LAYOUT_SCHEMA_VERSION = 2

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
    """Parse-provenance quality of an element — a faithfulness input (§1, §3.1, G1.0/G1.5).

    Carried into ``Span.layout`` at parse time and **consumed** at propositionization:
    :func:`parse_quality_factor` maps it to a multiplicative faithfulness penalty
    (scanned/handwritten → lower faithfulness → provisional → triage, §3.1). ``None`` means
    "not asserted" — treated as ``DIGITAL`` (clean), an unknown parse is not penalised.
    """

    DIGITAL = "digital"
    OCR = "ocr"
    HANDWRITTEN = "handwritten"


# Per-quality faithfulness penalty (§1 "parse quality = faithfulness input", §3.1). A proposition
# read off a degraded region is less trustworthy *at the source*, independent of whether the NLI
# verifier (G1.4) can re-confirm it from that same degraded text — so the penalty is a third
# factor in :func:`~iknos.types.epistemic.combine_faithfulness`, beside the verify and agreement
# signals. **Placeholder calibration constants** (a Trial-A5 target, like
# ``_POLARITY_DROP_FACTOR`` / the verdict bands): DIGITAL/unknown is the identity (no penalty),
# clean printed-text OCR is a mild discount, handwritten OCR a severe one (genuinely unreliable).
# Ordered by severity so :func:`worst_source_quality` can pick the worst region. The single place
# the policy lives, so calibration re-points it without touching the propositionizer.
_SOURCE_QUALITY_FACTOR: dict[SourceQuality, float] = {
    SourceQuality.DIGITAL: 1.0,
    SourceQuality.OCR: 0.85,
    SourceQuality.HANDWRITTEN: 0.60,
}

# Severity order (best → worst) for reducing a multi-region span to one quality.
_SOURCE_QUALITY_SEVERITY: tuple[SourceQuality, ...] = (
    SourceQuality.DIGITAL,
    SourceQuality.OCR,
    SourceQuality.HANDWRITTEN,
)


def parse_quality_factor(quality: SourceQuality | None) -> float:
    """The faithfulness penalty ∈ [0, 1] for a source region's parse quality (§1, §3.1).

    ``None`` (quality not asserted) is the identity ``1.0`` — an unknown parse is assumed clean,
    never penalised (the same "undefined, not zero" discipline as ``effective_credibility``).
    The value folds into :func:`~iknos.types.epistemic.combine_faithfulness` as the third
    independent defect factor. Pure; the per-quality constants are the calibration seam
    (:data:`_SOURCE_QUALITY_FACTOR`).
    """
    if quality is None:
        return 1.0
    return _SOURCE_QUALITY_FACTOR[quality]  # fail-loud on a quality with no factor


def worst_source_quality(layout: dict[str, Any] | None) -> SourceQuality | None:
    """The **worst** (most-degraded) ``source_quality`` across a span's ``Span.layout`` regions.

    A span may overlap several parse regions (e.g. a clean paragraph adjacent to a scanned
    table), so its faithfulness penalty is governed by the *worst* region it draws on — the
    conservative reading (a span is only as trustworthy as its weakest source). Reads the opaque,
    versioned layout dict this module owns (:func:`layouts_for_spans` is the writer); null- and
    version-tolerant — a ``None`` layout (text-only / null parser), a missing/empty ``regions``
    list, or a region with no ``source_quality`` all yield ``None`` (unknown → unpenalised). An
    unrecognised quality string (a layout written under a newer vocabulary) is skipped rather
    than raising, so a metadata surprise never aborts ingest.
    """
    if not layout:
        return None
    worst: SourceQuality | None = None
    worst_rank = -1
    for region in layout.get("regions", ()):
        raw = region.get("source_quality")
        if raw is None:
            continue
        try:
            quality = SourceQuality(raw)
        except ValueError:
            continue
        rank = _SOURCE_QUALITY_SEVERITY.index(quality)
        if rank > worst_rank:
            worst, worst_rank = quality, rank
    return worst


@dataclass(frozen=True)
class TableCell:
    """One cell of a parsed table (G1.18, review A1).

    ``[start, end)`` is **element-relative**: it indexes into the parent
    :class:`ParseElement`'s own ``text`` (``element.text[start:end]`` *is* the cell's text),
    not the document blob — so a cell is position-independent exactly like its element, and
    its document-absolute provenance is resolved only at persistence
    (:func:`layouts_for_spans`), where the element's place in the reading-order text is known.

    ``row``/``col`` are 0-based grid coordinates; ``row_span``/``col_span`` (≥ 1) cover merged
    cells; ``is_header`` marks a header cell (the column/row semantics the Phase-2 extractor
    reads to ingest cells as observation-class propositions, §3.1). ``bbox`` is optional
    per-cell geometry **in the parent element's coordinate frame** — origin/page_size/unit are
    inherited, a cell never re-declares the frame — so a cell bbox is only meaningful, and only
    permitted, when the element itself carries a region (enforced by :class:`ParseElement`).

    This type validates only what is text-independent (offset ordering, non-negative grid
    coords, positive spans); cell-offset-vs-element-text bounds are checked by
    :class:`ParseElement` (which owns the text) and grid-fit/overlap by :class:`Table` (which
    owns the grid extent).
    """

    row: int
    col: int
    start: int
    end: int
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.end:
            raise ValueError(
                f"TableCell offsets must satisfy 0 <= start < end (got start={self.start}, "
                f"end={self.end})"
            )
        if self.row < 0 or self.col < 0:
            raise ValueError(f"TableCell row/col must be >= 0 (got row={self.row}, col={self.col})")
        if self.row_span < 1 or self.col_span < 1:
            raise ValueError(
                f"TableCell row_span/col_span must be >= 1 (got row_span={self.row_span}, "
                f"col_span={self.col_span})"
            )


@dataclass(frozen=True)
class Table:
    """The preserved 2-D structure of a ``ParseKind.TABLE`` element (G1.18, review A1).

    Without this a ``TABLE`` element is a flat char range and the row/column structure is
    destroyed at the Stage-0 trust boundary — the Phase-2 table extractor would have nothing
    to read but prose. ``cells`` need **not** tile the grid or the element text: sparse tables,
    and whitespace/separators between cells, are fine (this is *not* the strict element-tiling
    rule of :meth:`ParseResult.from_offsets`). They must only be *consistent*: every cell sits
    inside the ``n_rows × n_cols`` grid and no two cells claim the same grid position once
    ``row_span``/``col_span`` are expanded. Offset-vs-text validation belongs to the owning
    :class:`ParseElement`; this type owns only the (text-independent) grid.
    """

    n_rows: int
    n_cols: int
    cells: tuple[TableCell, ...]

    def __post_init__(self) -> None:
        if self.n_rows < 1 or self.n_cols < 1:
            raise ValueError(
                f"Table must have n_rows >= 1 and n_cols >= 1 (got {self.n_rows}x{self.n_cols})"
            )
        occupied: set[tuple[int, int]] = set()
        for i, cell in enumerate(self.cells):
            if cell.row + cell.row_span > self.n_rows or cell.col + cell.col_span > self.n_cols:
                raise ValueError(
                    f"TableCell[{i}] at ({cell.row}, {cell.col}) with span "
                    f"({cell.row_span}, {cell.col_span}) extends past the "
                    f"{self.n_rows}x{self.n_cols} grid"
                )
            for r in range(cell.row, cell.row + cell.row_span):
                for c in range(cell.col, cell.col + cell.col_span):
                    if (r, c) in occupied:
                        raise ValueError(
                            f"TableCell[{i}] claims grid position ({r}, {c}) already occupied "
                            "by another cell"
                        )
                    occupied.add((r, c))


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
    table: Table | None = None  # G1.18: 2-D structure, only on a TABLE element

    def __post_init__(self) -> None:
        # Empty text would create an ambiguous zero-width char range that no span can
        # unambiguously intersect; the null parser emits zero elements for empty input
        # rather than one empty element (see ``NullParser.parse_text``).
        if not self.text:
            raise ValueError("ParseElement.text must be non-empty")
        # A region is all-or-nothing: a bbox without a page (or vice versa) is unrenderable.
        if (self.page is None) != (self.bbox is None):
            raise ValueError("ParseElement page and bbox must be set together")
        # A bbox is meaningless without the frame it lives in: a re-rasterized page can only
        # be hit if we know the coordinate origin, the page extent, and the unit. That frame
        # is *not recoverable* once the parse is discarded, so it is mandatory the moment a
        # bbox exists — refuse a half-specified region rather than persist an unrenderable one
        # (the silent-corruption class G1.0 exists to prevent).
        if self.bbox is not None and (
            self.origin is None or self.page_size is None or self.unit is None
        ):
            raise ValueError(
                "ParseElement with a bbox must also set origin, page_size and unit "
                "(a bbox is unrenderable without its coordinate frame, which is not "
                "recoverable later)"
            )
        # A table payload describes a 2-D grid, which is only meaningful on a TABLE element;
        # accepting one on a paragraph would silently mislabel structure (G1.18).
        if self.table is not None:
            if self.kind is not ParseKind.TABLE:
                raise ValueError(
                    f"ParseElement.table is only valid on a TABLE element (kind={self.kind})"
                )
            # The element owns its text, so it is the validator for element-relative cell
            # offsets: a cell ending past this element's text would resolve to a foreign or
            # out-of-range slice once rebased to document coordinates at persistence.
            for i, cell in enumerate(self.table.cells):
                if cell.end > len(self.text):
                    raise ValueError(
                        f"TableCell[{i}] end {cell.end} exceeds the element text length "
                        f"{len(self.text)} — cell offsets are element-relative"
                    )
                # A cell bbox lives in the element's coordinate frame; with no element region
                # there is no frame to inherit, so the bbox would be unrenderable.
                if cell.bbox is not None and not self.has_region:
                    raise ValueError(
                        f"TableCell[{i}] carries a bbox but its element has no region — a cell "
                        "bbox shares the element's coordinate frame, which is absent"
                    )

    @property
    def has_region(self) -> bool:
        """True iff this element carries page geometry (→ contributes a layout region)."""
        return self.page is not None and self.bbox is not None


@dataclass(frozen=True)
class OffsetSpec:
    """A parser-supplied element described by a char range into the parser's text + geometry.

    The text+offsets shape a *real* parser emits: one reading-order text blob plus, per
    element, the ``[start, end)`` slice of that blob it occupies and the page region it came
    from — **not** a standalone text string (that would be a second, drift-prone source for
    the same characters). :meth:`ParseResult.from_offsets` slices the element text out of the
    blob at these offsets, so the element text and the offsets cannot disagree by construction.
    Geometry mirrors :class:`ParseElement` and is validated identically (all-or-nothing region,
    bbox implies its full coordinate frame).

    ``table`` (G1.18) is the optional structured grid for a ``TABLE`` element. Note the offset
    origins differ by design: the element's own ``start``/``end`` are **absolute** into the
    parser's text blob (they have to be, to tile it), whereas the table's cell offsets are
    **element-relative** (into ``text[start:end]``) — :class:`Table`/:class:`TableCell` are
    position-independent like :class:`ParseElement`, so :meth:`ParseResult.from_offsets` can
    attach the table to the sliced element without rebasing.
    """

    kind: ParseKind
    start: int
    end: int
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    origin: str | None = None
    page_size: tuple[float, float] | None = None
    unit: str | None = None
    source_quality: SourceQuality | None = None
    table: Table | None = None


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

    @classmethod
    def from_offsets(
        cls,
        text: str,
        specs: Sequence[OffsetSpec],
        *,
        parser_name: str,
        parser_version: str,
        parse_schema_version: int = PARSE_SCHEMA_VERSION,
    ) -> "ParseResult":
        """Build a result from a parser's reading-order text blob + per-element offsets.

        This is the entry point for a *real* parser (MinerU/Docling/Marker): it hands back
        one text blob plus, per element, the slice of that blob the element occupies and the
        page region it came from. We **slice** each element's text out of ``text`` at its
        offsets — never trust a separately-supplied element string — so element text and
        offsets are one source by construction, and the returned result behaves exactly like
        any other (``.text`` is the element-join, ``char_ranges()`` are re-derived).

        Validation is **fail-loud at the trust boundary** (a parser is external/untrusted):

        - each ``[start, end)`` lies within ``text`` with ``start < end`` (no empty element);
        - specs are in reading order and **non-overlapping** (``start >= previous end``);
        - every inter-element gap (and any head/tail remainder) is **whitespace-only** — a gap
          holding real characters means the parser left text unassigned to any element, which
          would silently vanish from the reading-order text and from every span's provenance;
        - geometry completeness is enforced by :class:`ParseElement` (a bbox implies its frame);
        - a ``table`` payload's grid is validated by :class:`Table` (cells fit the grid, none
          overlap) and its element-relative cell offsets by :class:`ParseElement` (within the
          sliced element text) — both at construction, so a malformed table fails loud here.

        Raises :class:`ValueError` on any violation; the caller (the HTTP client) surfaces it
        as a hard parse failure rather than persisting a subtly-wrong parse.
        """
        elements: list[ParseElement] = []
        cursor = 0
        for i, spec in enumerate(specs):
            if not 0 <= spec.start < spec.end <= len(text):
                raise ValueError(
                    f"OffsetSpec[{i}] range ({spec.start}, {spec.end}) is out of bounds or "
                    f"empty for text of length {len(text)}"
                )
            if spec.start < cursor:
                raise ValueError(
                    f"OffsetSpec[{i}] starts at {spec.start} before the previous element ended "
                    f"at {cursor} — specs must be in reading order and non-overlapping"
                )
            if text[cursor : spec.start].strip():
                raise ValueError(
                    f"non-whitespace text between OffsetSpec[{i - 1}] and OffsetSpec[{i}] "
                    "would be dropped from the reading-order text — the parser left it "
                    "unassigned to any element"
                )
            elements.append(
                ParseElement(
                    kind=spec.kind,
                    text=text[spec.start : spec.end],
                    page=spec.page,
                    bbox=spec.bbox,
                    origin=spec.origin,
                    page_size=spec.page_size,
                    unit=spec.unit,
                    source_quality=spec.source_quality,
                    # Cell offsets are element-relative, so the table attaches to the sliced
                    # element with no rebasing; ParseElement re-validates them against the
                    # slice length (the trust-boundary check the gap plan asks for here).
                    table=spec.table,
                )
            )
            cursor = spec.end
        if text[cursor:].strip():
            raise ValueError(
                "non-whitespace text after the last element would be dropped from the "
                "reading-order text — the parser left a trailing remainder unassigned"
            )
        return cls(
            elements=tuple(elements),
            parser_name=parser_name,
            parser_version=parser_version,
            parse_schema_version=parse_schema_version,
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

    async def parse(self, document_bytes: bytes, *, media_type: str) -> ParseResult:
        """Satisfy the :class:`Parser` protocol: decode UTF-8 bytes and wrap as text.

        Lets the bytes-in ingest path degrade to the identity parser when no parse service is
        configured (``parser_base_url`` empty) — plain-text files still ingest. ``media_type``
        is accepted for protocol parity but not consulted: the null parser only does text, so
        non-text bytes raise :class:`UnicodeDecodeError` (fail loud) rather than producing
        mojibake. The decode is strict for exactly that reason.
        """
        return self.parse_text(document_bytes.decode("utf-8"))

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
    element carries geometry **or** a table — including the entire null-parser case
    (text-only elements), which reproduces the pre-Stage-0 layout-less behaviour exactly.

    A ``TABLE`` element also emits its structured ``table`` payload (G1.18). This is the
    point where cell offsets are rebased from **element-relative** to **document-absolute**
    (``el_start + cell.start``) — i.e. into ``result.text`` / ``document_content.raw_text``,
    the same coordinate system spans live in — so the Phase-2 consumer can resolve each cell's
    text and intersect cell ranges with the span. The whole table is carried on every
    overlapping span's region (it is bounded); the consumer dedupes by table and clips to the
    span. Because a table can matter even without page geometry, a table-bearing element
    contributes a region entry even when ``has_region`` is false (the geometry keys are then
    null — layout schema v2).

    The returned dict is the versioned, opaque-to-the-DB layout shape (see module
    docstring): ``{layout_schema_version, parser, regions:[{page, bbox, origin, page_size,
    unit, source_quality, table?}, ...]}``.
    """
    ranges = result.char_ranges()
    out: list[dict[str, Any] | None] = []
    for span_start, span_end in char_spans:
        regions: list[dict[str, Any]] = []
        for el, (el_start, el_end) in zip(result.elements, ranges, strict=True):
            # An element contributes iff it carries geometry or a table (G1.18) — a bare
            # text element still maps to no region, exactly as before.
            if not (el.has_region or el.table is not None):
                continue
            # Non-empty overlap of [span_start, span_end) and [el_start, el_end).
            if max(span_start, el_start) < min(span_end, el_end):
                region: dict[str, Any] = {
                    "page": el.page,
                    "bbox": list(el.bbox) if el.bbox is not None else None,
                    "origin": el.origin,
                    "page_size": list(el.page_size) if el.page_size is not None else None,
                    "unit": el.unit,
                    "source_quality": el.source_quality.value if el.source_quality else None,
                }
                if el.table is not None:
                    region["table"] = {
                        "n_rows": el.table.n_rows,
                        "n_cols": el.table.n_cols,
                        "cells": [
                            {
                                "row": cell.row,
                                "col": cell.col,
                                "row_span": cell.row_span,
                                "col_span": cell.col_span,
                                "is_header": cell.is_header,
                                # Rebased element-relative -> document-absolute (into raw_text).
                                "start": el_start + cell.start,
                                "end": el_start + cell.end,
                                "bbox": list(cell.bbox) if cell.bbox is not None else None,
                            }
                            for cell in el.table.cells
                        ],
                    }
                regions.append(region)
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
