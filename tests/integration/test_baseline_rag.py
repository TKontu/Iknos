"""Live-DB ingest + cosine retrieval for the plain-RAG baseline (Trial V4).

Exercises the part that needs a real pgvector database: ``baseline_chunks`` persistence and the
``<=>`` cosine top-k query. A deterministic fake embedder (no model) and a whitespace tokenizer
keep the test about the DB round-trip and ranking, not bge-m3 — so it is fast and the assertions
are exact. Requires DATABASE_URL (the `tests` CI workflow); the autouse `_isolate_db` fixture
truncates `baseline_chunks` between tests.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from iknos.baselines.rag import RagBaseline, baseline_document_uuid
from iknos.db.orm import BaselineChunk

pytestmark = pytest.mark.asyncio

# A tiny deterministic embedding space: each marker word owns one dimension. A text's vector is
# the (normalized) sum of its markers' basis vectors, so a query sharing a marker with a chunk is
# cosine-closest to it. No model — the DB does the ranking, which is what this test checks.
_MARKERS = ["lubrication", "overload", "counterfeit", "installation"]


class _MarkerEmbedder:
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * 1024
            present = [i for i, m in enumerate(_MARKERS) if m in text.lower()]
            for i in present:
                vec[i] = 1.0
            norm = len(present) ** 0.5 or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class _WhitespaceTokenizer:
    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]


@pytest_asyncio.fixture
async def rig(database_url: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(database_url)
    session_local = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield RagBaseline(
        embedder=_MarkerEmbedder(),
        llm=None,  # type: ignore[arg-type]  # retrieval path does not touch the LLM
        session_factory=session_local,
        tokenizer=_WhitespaceTokenizer(),
        model_name="fake-marker-embedder",
        top_k=2,
        chunk_tokens=64,
        overlap_tokens=8,
    )
    await engine.dispose()


async def test_ingest_then_retrieve_ranks_by_cosine(rig: RagBaseline) -> None:
    await rig.ingest_document("d-lube", "The lubrication film was lost on the bearing.")
    await rig.ingest_document("d-load", "There was no overload on the drivetrain.")

    results = await rig.retrieve("what caused the lubrication problem")
    assert results, "retrieval returned nothing"
    # The lubrication chunk shares the 'lubrication' marker with the query -> distance ~0, ranked
    # first; the overload chunk shares no marker -> orthogonal -> distance ~1.
    assert "lubrication" in results[0].text.lower()
    assert results[0].distance < results[-1].distance


async def test_ingest_is_idempotent(rig: RagBaseline, session: AsyncSession) -> None:
    n_first = await rig.ingest_document("d-lube", "The lubrication film was lost.")
    n_second = await rig.ingest_document("d-lube", "The lubrication film was lost.")
    assert n_first == n_second

    count = await session.scalar(
        select(func.count())
        .select_from(BaselineChunk)
        .where(BaselineChunk.document_id == baseline_document_uuid("d-lube"))
    )
    assert count == n_first  # re-ingest overwrote, did not duplicate


async def _chunk_count(session: AsyncSession, document_id: str) -> int:
    return await session.scalar(  # type: ignore[return-value]
        select(func.count())
        .select_from(BaselineChunk)
        .where(BaselineChunk.document_id == baseline_document_uuid(document_id))
    )


def _make_rig(database_url: str) -> RagBaseline:
    engine = create_async_engine(database_url)
    session_local = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return RagBaseline(
        embedder=_MarkerEmbedder(),
        llm=None,  # type: ignore[arg-type]
        session_factory=session_local,
        tokenizer=_WhitespaceTokenizer(),
        model_name="fake-marker-embedder",
        top_k=5,
        chunk_tokens=64,
        overlap_tokens=8,
    )


async def test_shrinking_reingest_drops_orphan_tail_chunks(
    rig: RagBaseline, session: AsyncSession
) -> None:
    long_text = " ".join(f"w{i}" for i in range(200))  # several 64-token chunks (stride 56)
    n_long = await rig.ingest_document("d-shrink", long_text)
    assert n_long > 1, "fixture must produce multiple chunks to exercise the orphan path"
    assert await _chunk_count(session, "d-shrink") == n_long

    short_text = "the lubrication film was lost on the bearing"
    n_short = await rig.ingest_document("d-shrink", short_text)
    assert n_short < n_long
    # The tail chunks of the long version must be deleted, not orphaned.
    assert await _chunk_count(session, "d-shrink") == n_short
    results = await rig.retrieve("w150")
    leftover = [r for r in results if r.document_id == str(baseline_document_uuid("d-shrink"))]
    assert all("w150" not in r.text for r in leftover)


async def test_emptied_reingest_clears_all_chunks(rig: RagBaseline, session: AsyncSession) -> None:
    n = await rig.ingest_document("d-empty", " ".join(f"w{i}" for i in range(120)))
    assert n > 0
    n_empty = await rig.ingest_document("d-empty", "   \n\t  ")  # whitespace → no tokens
    assert n_empty == 0
    assert await _chunk_count(session, "d-empty") == 0


async def test_retrieval_is_scoped_to_the_ingested_corpus(database_url: str) -> None:
    # Two rigs (same model, same baseline_chunks table) over two disjoint corpora: rig B must not
    # retrieve rig A's chunks, even though `model` matches — only document-set scoping prevents the
    # cross-corpus contamination the V12 review flagged (a chunk from another corpus getting cited).
    rig_a = _make_rig(database_url)
    rig_b = _make_rig(database_url)
    await rig_a.ingest_document("corpusA-lube", "The lubrication film was lost on the bearing.")
    await rig_b.ingest_document("corpusB-load", "There was no overload on the drivetrain.")

    # rig B asks a lubrication question — corpus A has the only lubrication chunk, but rig B did
    # not ingest it, so retrieval must not return it.
    results_b = await rig_b.retrieve("what caused the lubrication problem")
    a_doc = str(baseline_document_uuid("corpusA-lube"))
    assert all(r.document_id != a_doc for r in results_b), "cross-corpus contamination in retrieval"
    # rig A still finds its own lubrication chunk.
    results_a = await rig_a.retrieve("what caused the lubrication problem")
    assert any("lubrication" in r.text.lower() for r in results_a)
