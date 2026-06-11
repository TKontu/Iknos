"""Integration test: span persistence end to end (G1.9) against live AGE+pgvector.

Proves the segmentation-output → storage write path: `Span` vertices and
`document_embeddings` rows are written idempotently, the span text resolves via the
local join, the content-hash guard makes a re-run a no-op and fails loud on changed
inputs, whitespace spans are dropped, optional `Span.layout` is persisted, and the
proposition layer runs against the *persisted* spans (closing the original gap where
the proposition test hand-created them).

The embedding substrate is not loaded (no model download): persistence is torch-free
and takes precomputed vectors, so we pass fake 1024-d vectors, exactly as the
proposition integration test mocks its substrate.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.ingest import (
    DocumentResegmentationError,
    persist_spans,
    span_content_hash,
)
from iknos.core.proposition import Propositionizer
from iknos.db.age import bootstrap_session, execute_cypher
from iknos.db.spans import resolve_span_text

pytestmark = pytest.mark.asyncio

_PARAMS = {"max_len": 10, "penalty_weight": 0.1, "density_weight": 0.5}
_MODEL = "test-model"
_VEC = [0.05] * 1024  # nonzero → a real span
_ZERO = [0.0] * 1024  # whitespace span → skipped


async def _seed_document(session: AsyncSession, raw: str) -> uuid.UUID:
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": raw},
    )
    return doc_id


async def _count(session: AsyncSession, query: str) -> int:
    rows = await execute_cypher(session, query, returns="n agtype")
    return int(rows[0][0]) if rows else 0


async def _emb_count(session: AsyncSession, doc_id: uuid.UUID) -> int:
    r = await session.execute(
        text("SELECT count(*) FROM document_embeddings WHERE document_id = :d"), {"d": doc_id}
    )
    return int(r.scalar_one())


async def test_persist_spans_end_to_end(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "First sentence here. Second sentence follows."
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, 20), (21, len(raw))]
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)

    result = await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, _VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    assert result.skipped == 0
    assert result.embedding_rows == 2
    assert len(result.spans) == 2

    # Span vertices, document-scoped, each carrying level + sensitivity.
    assert await _count(session, f"MATCH (s:Span {{document_id: '{doc_id}'}}) RETURN count(s)") == 2
    lvl = await execute_cypher(
        session,
        f"MATCH (s:Span {{document_id: '{doc_id}'}}) RETURN s.level, s.sensitivity_level LIMIT 1",
        returns="level agtype, sens agtype",
    )
    assert int(lvl[0][0]) == 0
    assert str(lvl[0][1]).strip('"') == "public"

    # Dense rows: one per span, 1024-dim, span_id matches the deterministic vertex id.
    assert await _emb_count(session, doc_id) == 2
    dims = await session.execute(
        text(
            "SELECT DISTINCT vector_dims(embedding) FROM document_embeddings WHERE document_id=:d"
        ),
        {"d": doc_id},
    )
    assert dims.scalar_one() == 1024
    for s in result.spans:
        r = await session.execute(
            text("SELECT count(*) FROM document_embeddings WHERE span_id = :sid"), {"sid": s.id}
        )
        assert r.scalar_one() == 1

    # resolve_span_text round-trip via the local join.
    for s in result.spans:
        assert await resolve_span_text(session, doc_id, s.start, s.end) == raw[s.start : s.end]


async def test_reload_is_a_no_op(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "Alpha statement. Beta statement."
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, 16), (17, len(raw))]
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)

    first = await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, _VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    # Capture the dense row ids + the segment-action count after the first load.
    ids_before = (
        await session.execute(
            text(
                "SELECT span_id, id FROM document_embeddings WHERE document_id=:d ORDER BY span_id"
            ),
            {"d": doc_id},
        )
    ).all()
    actions_before = (
        await session.execute(
            text(
                "SELECT count(*) FROM actions WHERE actor='segmenter' AND inputs->>'document_id'=:d"
            ),
            {"d": str(doc_id)},
        )
    ).scalar_one()

    second = await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, _VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    # No-op: guard short-circuits, no writes, spans still reported for the caller.
    assert second.already_segmented is True
    assert second.embedding_rows == 0
    assert [s.id for s in second.spans] == [s.id for s in first.spans]

    # Dup-free + stable row ids (the assertion whose absence hid the pack bug).
    assert await _count(session, f"MATCH (s:Span {{document_id: '{doc_id}'}}) RETURN count(s)") == 2
    assert await _emb_count(session, doc_id) == 2
    ids_after = (
        await session.execute(
            text(
                "SELECT span_id, id FROM document_embeddings WHERE document_id=:d ORDER BY span_id"
            ),
            {"d": doc_id},
        )
    ).all()
    assert ids_after == ids_before  # row ids unchanged → no churn
    actions_after = (
        await session.execute(
            text(
                "SELECT count(*) FROM actions WHERE actor='segmenter' AND inputs->>'document_id'=:d"
            ),
            {"d": str(doc_id)},
        )
    ).scalar_one()
    assert actions_after == actions_before  # no second Action


async def test_resegmentation_with_changed_inputs_fails_loud(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "Original text body."
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, len(raw))]
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)
    await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    changed = span_content_hash("DIFFERENT body", segmenter_params=_PARAMS, model=_MODEL)
    with pytest.raises(DocumentResegmentationError, match="immutable"):
        await persist_spans(
            session,
            doc_id,
            char_spans,
            [_VEC],
            content_hash=changed,
            segmenter_params=_PARAMS,
            model=_MODEL,
        )
    await session.rollback()

    # Fail-loud, not silent divergence: nothing extra written.
    assert await _emb_count(session, doc_id) == 1


async def test_whitespace_span_is_skipped(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "Real claim sentence.\n\n   \n"
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, 20), (20, len(raw))]  # second is whitespace
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)

    result = await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, _ZERO],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    assert result.skipped == 1
    assert len(result.spans) == 1
    assert await _count(session, f"MATCH (s:Span {{document_id: '{doc_id}'}}) RETURN count(s)") == 1
    assert await _emb_count(session, doc_id) == 1


async def test_layout_is_persisted_when_supplied(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "Located claim. Plain claim."
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, 14), (15, len(raw))]
    layout = {"page": 3, "bbox": [10, 20, 100, 40]}
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)

    result = await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, _VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
        layouts=[layout, None],
    )
    await session.commit()

    assert result.spans[0].layout == layout
    assert result.spans[1].layout is None
    # The located span carries a layout property; the plain one does not.
    rows = await execute_cypher(
        session,
        f"MATCH (s:Span {{id: '{result.spans[0].id}'}}) RETURN s.layout",
        returns="layout agtype",
    )
    assert "page" in str(rows[0][0]) and "bbox" in str(rows[0][0])
    plain = await execute_cypher(
        session,
        f"MATCH (s:Span {{id: '{result.spans[1].id}'}}) RETURN s.layout",
        returns="layout agtype",
    )
    assert plain[0][0] is None


def _mock_propositionizer(texts: list[str]) -> Propositionizer:
    llm = MagicMock()
    llm.model = _MODEL
    llm.guided_complete = AsyncMock(return_value={"propositions": [{"text": t} for t in texts]})
    substrate = MagicMock()
    substrate.model_name = _MODEL  # vector-space identity written on each proposition row (G1.16)
    substrate.embed_passages = MagicMock(return_value=[[0.2] * 1024 for _ in texts])
    return Propositionizer(llm, substrate, context_window=8, concurrency=2)


async def test_propositionizer_runs_against_persisted_spans(session: AsyncSession) -> None:
    """Closes the original gap: no hand-created spans — the two layers compose."""
    await bootstrap_session(session)
    raw = "Smith reviewed the report. He found the budget insufficient."
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, 26), (27, len(raw))]
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)

    result = await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, _VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    p = _mock_propositionizer(["Smith found the budget insufficient."])
    await p.propositionize_document(session, doc_id, result.spans, raw)

    # Propositions link via EVIDENCED_BY to the *persisted* spans.
    for s in result.spans:
        rows = await execute_cypher(
            session,
            f"MATCH (:Proposition)-[:EVIDENCED_BY]->(s:Span {{id: '{s.id}'}}) RETURN count(*)",
            returns="n agtype",
        )
        assert int(rows[0][0]) >= 1
