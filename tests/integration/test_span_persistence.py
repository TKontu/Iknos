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

from iknos.core.embeddings import EmbeddingModelMismatchError
from iknos.core.ingest import (
    DocumentResegmentationError,
    assert_embedding_model_compatible,
    load_document_spans,
    load_document_text,
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


async def test_load_document_spans_reloads_extraction_level_in_order(session: AsyncSession) -> None:
    # The read-path inverse of persist_spans the R11 propositionize job uses (it has no in-memory
    # spans, only a document id). Must return level-0 spans only (the extraction granularity),
    # ordered by start, with the JSON-encoded layout decoded back to a dict.
    await bootstrap_session(session)
    raw = "Gamma one. Gamma two. Gamma three."  # offsets: (0,10) (11,21) (22,34)
    doc_id = await _seed_document(session, raw)

    # Persist level-0 spans OUT of start order to prove the reloader sorts them; the first carries
    # a layout, the others none.
    fine = [(11, 21), (0, 10), (22, len(raw))]
    layout = {"page": 1, "bbox": [0.0, 0.0, 1.0, 1.0]}
    ch0 = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)
    await persist_spans(
        session,
        doc_id,
        fine,
        [_VEC, _VEC, _VEC],
        content_hash=ch0,
        segmenter_params=_PARAMS,
        model=_MODEL,
        level=0,
        layouts=[layout, None, None],
    )
    # A coarser level-1 span the extractor must NOT see.
    coarse_params = {**_PARAMS, "max_len": 40}
    ch1 = span_content_hash(raw, segmenter_params=coarse_params, model=_MODEL)
    await persist_spans(
        session,
        doc_id,
        [(0, len(raw))],
        [_VEC],
        content_hash=ch1,
        segmenter_params=coarse_params,
        model=_MODEL,
        level=1,
    )
    await session.commit()

    spans = await load_document_spans(session, doc_id)
    assert [s.level for s in spans] == [0, 0, 0]  # level-1 excluded
    assert [s.start for s in spans] == [0, 11, 22]  # sorted by start despite insert order
    assert all(s.document_id == doc_id for s in spans)
    by_start = {s.start: s for s in spans}
    assert by_start[11].layout == layout  # JSON-encoded property decoded back to a dict
    assert by_start[0].layout is None
    # Span text resolves via the offsets the reloader carries.
    assert raw[by_start[0].start : by_start[0].end] == "Gamma one."

    assert await load_document_text(session, doc_id) == raw
    # A never-ingested document is a clean no-op (the job returns without calling the LLM).
    assert await load_document_spans(session, uuid.uuid4()) == []
    assert await load_document_text(session, uuid.uuid4()) is None


async def test_segment_action_duration_includes_stage_setup(session: AsyncSession) -> None:
    """R12: the segment Action's duration_ms folds in ``stage_setup_ms`` — the embed/split/segment
    cost the ingest entry point already paid — not just the persist write, so the dominant
    embedding cost is attributed. Direct callers passing the default 0 record persist time alone.
    """
    await bootstrap_session(session)
    raw = "Solo claim sentence."
    doc_id = await _seed_document(session, raw)
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)
    await persist_spans(
        session,
        doc_id,
        [(0, len(raw))],
        [_VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
        stage_setup_ms=1000,
    )
    await session.commit()

    row = await session.execute(
        text(
            "SELECT metrics->>'duration_ms', metrics->>'n_spans', "
            "metrics->>'n_skipped_whitespace' FROM actions "
            "WHERE actor='segmenter' AND inputs->>'document_id'=:d"
        ),
        {"d": str(doc_id)},
    )
    dur, n_spans, n_skipped = row.one()
    assert int(dur) >= 1000  # the threaded setup cost is included, not dropped
    assert int(n_spans) == 1
    assert int(n_skipped) == 0


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


async def test_skip_reasons_are_audited_per_reason(session: AsyncSession) -> None:
    """W11: a whitespace span (pooled to ``None``) and a zero-vector span (the legacy sentinel) are
    counted under *different* reasons on the result and the segment Action — not lumped as one
    ``skipped`` total — so triage tells the expected whitespace drop from a pooling regression."""
    await bootstrap_session(session)
    raw = "Real claim sentence. ws zv"
    doc_id = await _seed_document(session, raw)
    # span 0 real, span 1 whitespace (None embedding), span 2 zero-vector.
    char_spans = [(0, 20), (21, 23), (24, 26)]
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)

    result = await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, None, _ZERO],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    assert result.skipped == 2  # the total is preserved
    assert result.skipped_whitespace == 1
    assert result.skipped_zero_vector == 1
    assert len(result.spans) == 1

    row = await session.execute(
        text(
            "SELECT metrics->>'n_skipped_whitespace', metrics->>'n_skipped_zero_vector', "
            "outputs->'skipped_by_reason' FROM actions WHERE action_type = 'segment' "
            "AND inputs->>'document_id' = :d"
        ),
        {"d": str(doc_id)},
    )
    n_ws, n_zv, by_reason = row.one()
    assert int(n_ws) == 1 and int(n_zv) == 1
    assert by_reason == {"whitespace": 1, "zero_vector": 1}


async def test_early_model_guard_refuses_a_swap_before_embedding(session: AsyncSession) -> None:
    """W11: ``assert_embedding_model_compatible`` is the early, *global* G1.16 guard — once the
    dense index holds one model, a different backend identity is refused (fail-fast, before any
    embed), and it catches the new-document hole the per-document persist_spans guard cannot see."""
    await bootstrap_session(session)
    raw = "A grounded claim."
    doc_id = await _seed_document(session, raw)
    ch = span_content_hash(raw, segmenter_params=_PARAMS, model=_MODEL)
    await persist_spans(
        session,
        doc_id,
        [(0, len(raw))],
        [_VEC],
        content_hash=ch,
        segmenter_params=_PARAMS,
        model=_MODEL,
    )
    await session.commit()

    # Same identity / empty-index → no-op; a different identity → fail loud (even for a *new* doc,
    # whose own rows are empty, so the per-document guard would have missed the silent mixing).
    await assert_embedding_model_compatible(session, _MODEL)
    with pytest.raises(EmbeddingModelMismatchError):
        await assert_embedding_model_compatible(session, "some-other-model")


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
