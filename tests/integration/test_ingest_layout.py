"""Integration test: Stage 0 parse front-end wired through ingest (G1.0), live AGE+pgvector.

Proves the end-to-end ``ingest_document`` path with the identity (null) parser: a parse
Action is recorded, spans persist with ``layout=None`` (plain-text mode), the run is
idempotent, and — the G1.0 robustness point (D) — the parse content hash now participates
in the segmentation immutability guard, so a changed parse identity over identical text
fails loud instead of silently serving stale layout.

The embedding substrate and segmenter are mocked (no model download): ``ingest_document``
takes precomputed vectors via the substrate's context, exactly as the proposition and
span-persistence integration tests mock theirs.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.ingest import (
    DocumentResegmentationError,
    ingest_document,
    ingest_document_bytes,
    persist_spans,
    span_content_hash,
)
from iknos.core.parse import NullParser, OffsetSpec, ParseKind, ParseResult, SourceQuality
from iknos.db.age import bootstrap_session, execute_cypher
from iknos.db.spans import resolve_span_text

pytestmark = pytest.mark.asyncio

_PARAMS = {"max_len": 10, "penalty_weight": 0.1, "density_weight": 0.5}
_MODEL = "test-model"
_VEC = [0.05] * 1024


async def _seed_document(session: AsyncSession, raw: str) -> uuid.UUID:
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": raw},
    )
    return doc_id


async def _action_count(session: AsyncSession, actor: str, doc_id: uuid.UUID) -> int:
    r = await session.execute(
        text("SELECT count(*) FROM actions WHERE actor=:a AND inputs->>'document_id'=:d"),
        {"a": actor, "d": str(doc_id)},
    )
    return int(r.scalar_one())


def _mock_substrate(vecs: list[list[float]]) -> MagicMock:
    """A substrate whose context yields the given pooled vectors, one per span in order."""
    context = MagicMock()
    context.pool_span = MagicMock(side_effect=list(vecs))
    # G1.13 slice 2: _ingest_parsed reads the windowing policy (→ span_content_hash) and the
    # window layout (→ segment Action). Real JSON-serializable dicts, not MagicMocks.
    _policy = {"overlap": 1024, "model_max_tokens": 8192, "window_token_size": 8190}
    context.windowing_policy = MagicMock(return_value=_policy)
    context.window_layout = MagicMock(return_value={**_policy, "count": 1, "boundaries": [[0, 1]]})
    substrate = MagicMock()
    substrate.model_name = _MODEL
    substrate.embed_document = MagicMock(return_value=context)
    return substrate


def _mock_segmenter(char_spans: list[tuple[int, int]]) -> MagicMock:
    seg = MagicMock()
    seg.segment_document = MagicMock(return_value=char_spans)
    seg.max_len = _PARAMS["max_len"]
    seg.penalty_weight = _PARAMS["penalty_weight"]
    seg.density_weight = _PARAMS["density_weight"]
    return seg


async def test_ingest_document_records_parse_action_and_null_layout(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "First claim sentence. Second claim sentence."
    doc_id = uuid.uuid4()
    char_spans = [(0, 21), (21, len(raw))]

    result = await ingest_document(
        session,
        doc_id,
        raw,
        _mock_substrate([_VEC, _VEC]),
        _mock_segmenter(char_spans),
    )
    await session.commit()

    assert len(result.spans) == 2
    # Null parser → plain-text mode → no layout on any span (pre-Stage-0 behaviour).
    assert all(s.layout is None for s in result.spans)

    # A parse Action was recorded, carrying the parse content hash + parser identity.
    assert await _action_count(session, "parser", doc_id) == 1
    assert await _action_count(session, "segmenter", doc_id) == 1
    parse_row = await session.execute(
        text(
            "SELECT inputs->>'content_hash', inputs->>'parser_name', inputs->>'media_type' "
            "FROM actions WHERE actor='parser' AND inputs->>'document_id'=:d"
        ),
        {"d": str(doc_id)},
    )
    content_hash, parser_name, media_type = parse_row.one()
    assert content_hash and len(content_hash) == 64
    assert parser_name == "null"
    assert media_type == "text/plain"

    # Spans carry no layout property in the graph either.
    rows = await execute_cypher(
        session,
        f"MATCH (s:Span {{id: '{result.spans[0].id}'}}) RETURN s.layout",
        returns="layout agtype",
    )
    assert rows[0][0] is None

    for s in result.spans:
        assert await resolve_span_text(session, doc_id, s.start, s.end) == raw[s.start : s.end]


async def test_ingest_document_is_idempotent(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "Alpha statement here. Beta statement here."
    doc_id = uuid.uuid4()
    char_spans = [(0, 21), (21, len(raw))]

    first = await ingest_document(
        session, doc_id, raw, _mock_substrate([_VEC, _VEC]), _mock_segmenter(char_spans)
    )
    await session.commit()
    assert first.already_segmented is False

    # Re-ingest the same document with the same inputs (fresh mocks; vectors re-supplied).
    second = await ingest_document(
        session, doc_id, raw, _mock_substrate([_VEC, _VEC]), _mock_segmenter(char_spans)
    )
    await session.commit()

    # True no-op: guard short-circuits, and neither stage records a second Action.
    assert second.already_segmented is True
    assert second.embedding_rows == 0
    assert [s.id for s in second.spans] == [s.id for s in first.spans]
    assert await _action_count(session, "parser", doc_id) == 1
    assert await _action_count(session, "segmenter", doc_id) == 1


async def test_parse_hash_participates_in_resegmentation_guard(session: AsyncSession) -> None:
    """G1.0 (D): identical text but a different parse identity must re-segment, not skip.

    Two parsers can yield the *same* reading-order text but different layout. Because the
    parse hash now folds into ``span_content_hash``, the second persist sees a different
    segmentation identity and fails loud — closing the silent-stale-layout hole.
    """
    await bootstrap_session(session)
    raw = "Stable reading-order text."
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, len(raw))]

    ch_a = span_content_hash(
        raw, segmenter_params=_PARAMS, model=_MODEL, parse_content_hash="parse-identity-A"
    )
    await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC],
        content_hash=ch_a,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    # Same raw text / segmenter / model — only the upstream parse identity differs.
    ch_b = span_content_hash(
        raw, segmenter_params=_PARAMS, model=_MODEL, parse_content_hash="parse-identity-B"
    )
    assert ch_b != ch_a
    with pytest.raises(DocumentResegmentationError, match="immutable"):
        await persist_spans(
            session,
            doc_id,
            char_spans,
            [_VEC],
            content_hash=ch_b,
            segmenter_params=_PARAMS,
            model=_MODEL,
        )
    await session.rollback()


class _StubLocatedParser:
    """A real-parser stand-in: returns two located elements via ``from_offsets`` (no MinerU
    service needed to prove the bytes path threads real ``{page, bbox}`` geometry through)."""

    async def parse(self, document_bytes: bytes, *, media_type: str) -> ParseResult:
        blob = "Region one block.\n\nRegion two block."  # ranges: [0,17) gap [17,19) [19,36)
        specs = [
            OffsetSpec(
                kind=ParseKind.PARAGRAPH,
                start=0,
                end=17,
                page=1,
                bbox=(10, 20, 100, 40),
                origin="top-left",
                page_size=(612.0, 792.0),
                unit="pt",
                source_quality=SourceQuality.DIGITAL,
            ),
            OffsetSpec(
                kind=ParseKind.PARAGRAPH,
                start=19,
                end=36,
                page=2,
                bbox=(10, 60, 100, 80),
                origin="top-left",
                page_size=(612.0, 792.0),
                unit="pt",
                source_quality=SourceQuality.OCR,
            ),
        ]
        return ParseResult.from_offsets(blob, specs, parser_name="mineru", parser_version="2.1.0")


async def test_ingest_document_bytes_persists_real_layout(session: AsyncSession) -> None:
    """The bytes-in path: a located parser → spans carry real multi-page layout regions."""
    await bootstrap_session(session)
    doc_id = uuid.uuid4()
    blob = "Region one block.\n\nRegion two block."
    char_spans = [(0, 17), (19, 36)]  # one per located element

    result = await ingest_document_bytes(
        session,
        doc_id,
        b"%PDF-1.7 fake bytes",
        _mock_substrate([_VEC, _VEC]),
        _mock_segmenter(char_spans),
        media_type="application/pdf",
        parser=_StubLocatedParser(),
    )
    await session.commit()

    assert len(result.spans) == 2
    # raw_text is *derived* from the parse result (== the reading-order blob), so span text
    # resolves against it exactly.
    for s in result.spans:
        assert await resolve_span_text(session, doc_id, s.start, s.end) == blob[s.start : s.end]

    # Each span carries the right region: page + geometry + source quality.
    layout0 = result.spans[0].layout
    layout1 = result.spans[1].layout
    assert layout0 is not None and layout1 is not None
    assert layout0["parser"] == "mineru"
    assert layout0["regions"][0]["page"] == 1
    assert layout0["regions"][0]["source_quality"] == "digital"
    assert layout1["regions"][0]["page"] == 2
    assert layout1["regions"][0]["source_quality"] == "ocr"
    assert layout1["regions"][0]["unit"] == "pt"

    # The layout round-trips through the AGE graph (stored as a JSON property).
    rows = await execute_cypher(
        session,
        f"MATCH (s:Span {{id: '{result.spans[0].id}'}}) RETURN s.layout",
        returns="layout agtype",
    )
    assert rows[0][0] is not None

    # The parse Action records the *real* parser identity + media type (not text/plain).
    parse_row = await session.execute(
        text(
            "SELECT inputs->>'parser_name', inputs->>'parser_version', inputs->>'media_type' "
            "FROM actions WHERE actor='parser' AND inputs->>'document_id'=:d"
        ),
        {"d": str(doc_id)},
    )
    parser_name, parser_version, media_type = parse_row.one()
    assert (parser_name, parser_version, media_type) == ("mineru", "2.1.0", "application/pdf")


async def test_ingest_document_bytes_null_parser_degrades_to_no_layout(
    session: AsyncSession,
) -> None:
    """No service configured → the injected NullParser handles bytes: text ingests, no layout."""
    await bootstrap_session(session)
    doc_id = uuid.uuid4()
    raw = "Plain claim one. Plain claim two."
    char_spans = [(0, 16), (16, len(raw))]

    result = await ingest_document_bytes(
        session,
        doc_id,
        raw.encode("utf-8"),
        _mock_substrate([_VEC, _VEC]),
        _mock_segmenter(char_spans),
        media_type="text/plain",
        parser=NullParser(),
    )
    await session.commit()

    assert len(result.spans) == 2
    assert all(s.layout is None for s in result.spans)
    assert await _action_count(session, "parser", doc_id) == 1
    parse_row = await session.execute(
        text(
            "SELECT inputs->>'parser_name' FROM actions "
            "WHERE actor='parser' AND inputs->>'document_id'=:d"
        ),
        {"d": str(doc_id)},
    )
    assert parse_row.scalar_one() == "null"
