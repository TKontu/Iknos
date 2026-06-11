"""Integration test: reference-corpus read-only amortization (G1.8; §6.1), live AGE+pgvector.

Proves the two-regime ingest boundary §6.1 requires:

- A reference document ingests **once** into a reference-tier box: spans persist and the
  ``(:Document)-[:MEMBER_OF]->(:Box)`` read-only **seal** is recorded.
- Re-ingesting identical content is **amortized**: the whole pipeline is skipped (the
  substrate's ``embed_document`` is never called) and ``reused=True`` — a later
  investigation pays nothing to reuse the corpus.
- Changed content under the same id, or a different box, **fails loud**
  (``ReferenceSealError``) — a reference corpus is immutable per ``(id, content)``.
- A case/working box is refused (``ValueError``) — those are the per-investigation regime.

The embedding substrate and segmenter are mocked (no model download), exactly as
``test_ingest_layout.py`` mocks theirs.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import case_box, reference_box
from iknos.core.ingest import ingest_reference_document
from iknos.core.reference_corpus import ReferenceSealError, get_reference_seal
from iknos.core.segmentation import SegmentLevel
from iknos.db.age import bootstrap_session, execute_cypher
from iknos.types.nodes import Tier

pytestmark = pytest.mark.asyncio

_PARAMS = {"max_len": 10, "penalty_weight": 0.1, "density_weight": 0.5}
_MODEL = "test-model"
_VEC = [0.05] * 1024


def _mock_substrate(vecs: list[list[float]]) -> MagicMock:
    """A substrate whose context yields the given pooled vectors, one per span in order."""
    context = MagicMock()
    context.pool_span = MagicMock(side_effect=list(vecs))
    _policy = {"overlap": 1024, "model_max_tokens": 8192, "window_token_size": 8190}
    context.windowing_policy = MagicMock(return_value=_policy)
    context.window_layout = MagicMock(return_value={**_policy, "count": 1, "boundaries": [[0, 1]]})
    substrate = MagicMock()
    substrate.model_name = _MODEL
    substrate.embed_document = MagicMock(return_value=context)
    return substrate


def _mock_segmenter(char_spans: list[tuple[int, int]]) -> MagicMock:
    level0 = SegmentLevel(
        0, _PARAMS["max_len"], _PARAMS["penalty_weight"], _PARAMS["density_weight"]
    )
    seg = MagicMock()
    seg.segment_document_levels = MagicMock(return_value=[(level0, char_spans)])
    return seg


async def _span_count(session: AsyncSession, doc_id: uuid.UUID) -> int:
    rows = await execute_cypher(
        session,
        f"MATCH (s:Span {{document_id: '{doc_id}'}}) RETURN count(s)",
        returns="n agtype",
    )
    return int(rows[0][0]) if rows else 0


async def _seal_action_count(session: AsyncSession, doc_id: uuid.UUID) -> int:
    r = await session.execute(
        text(
            "SELECT count(*) FROM actions WHERE actor='reference-ingest' "
            "AND action_type='seal-reference' AND inputs->>'document_id'=:d"
        ),
        {"d": str(doc_id)},
    )
    return int(r.scalar_one())


_RAW = "First sentence here. Second sentence follows."
_SPANS = [(0, 20), (21, len(_RAW))]


async def test_first_ingest_persists_spans_and_seals(session: AsyncSession) -> None:
    await bootstrap_session(session)
    doc_id = uuid.uuid4()
    box = reference_box("pump-handbook", "1", "handbook.pdf", 0.9)

    out = await ingest_reference_document(
        session,
        doc_id,
        _RAW,
        _mock_substrate([_VEC, _VEC]),
        _mock_segmenter(_SPANS),
        box=box,
    )

    assert out.reused is False
    assert out.box_id == box.id
    assert out.result is not None and out.result.embedding_rows == 2
    assert await _span_count(session, doc_id) == 2

    seal = await get_reference_seal(session, doc_id)
    assert seal is not None
    assert seal.box_id == box.id
    assert seal.tier is Tier.REFERENCE
    assert await _seal_action_count(session, doc_id) == 1


async def test_reingest_identical_is_amortized_no_pipeline(session: AsyncSession) -> None:
    await bootstrap_session(session)
    doc_id = uuid.uuid4()
    box = reference_box("pump-handbook", "1", "handbook.pdf", 0.9)
    await ingest_reference_document(
        session, doc_id, _RAW, _mock_substrate([_VEC, _VEC]), _mock_segmenter(_SPANS), box=box
    )

    # A second "investigation" reuses the corpus. Hand it a substrate that would explode if
    # the pipeline ran — the amortization short-circuits *before* embed_document.
    reuse_substrate = _mock_substrate([_VEC, _VEC])
    out = await ingest_reference_document(
        session, doc_id, _RAW, reuse_substrate, _mock_segmenter(_SPANS), box=box
    )

    assert out.reused is True
    assert out.result is None
    reuse_substrate.embed_document.assert_not_called()
    # Nothing new written: spans unchanged, no second seal Action.
    assert await _span_count(session, doc_id) == 2
    assert await _seal_action_count(session, doc_id) == 1


async def test_changed_content_same_id_fails_loud(session: AsyncSession) -> None:
    await bootstrap_session(session)
    doc_id = uuid.uuid4()
    box = reference_box("pump-handbook", "1", "handbook.pdf", 0.9)
    await ingest_reference_document(
        session, doc_id, _RAW, _mock_substrate([_VEC, _VEC]), _mock_segmenter(_SPANS), box=box
    )

    changed = _RAW + " A third sentence appears."
    with pytest.raises(ReferenceSealError, match="immutable"):
        await ingest_reference_document(
            session,
            doc_id,
            changed,
            _mock_substrate([_VEC, _VEC, _VEC]),
            _mock_segmenter([*_SPANS, (len(_RAW) + 1, len(changed))]),
            box=box,
        )


async def test_reseal_into_different_box_fails_loud(session: AsyncSession) -> None:
    await bootstrap_session(session)
    doc_id = uuid.uuid4()
    box_a = reference_box("pump-handbook", "1", "handbook.pdf", 0.9)
    box_b = reference_box("valve-handbook", "1", "valves.pdf", 0.9)
    await ingest_reference_document(
        session, doc_id, _RAW, _mock_substrate([_VEC, _VEC]), _mock_segmenter(_SPANS), box=box_a
    )

    with pytest.raises(ReferenceSealError, match="already sealed into"):
        await ingest_reference_document(
            session, doc_id, _RAW, _mock_substrate([_VEC, _VEC]), _mock_segmenter(_SPANS), box=box_b
        )


async def test_case_box_is_refused(session: AsyncSession) -> None:
    await bootstrap_session(session)
    doc_id = uuid.uuid4()
    case = case_box("incident-42", "1", "incident.pdf", 0.6)

    # The pure tier guard fires before any DB work — a case box is the per-investigation
    # regime (ingest_document), never amortized read-only.
    with pytest.raises(ValueError, match="reference/schema-tier"):
        await ingest_reference_document(
            session, doc_id, _RAW, _mock_substrate([_VEC, _VEC]), _mock_segmenter(_SPANS), box=case
        )
    assert await _span_count(session, doc_id) == 0
