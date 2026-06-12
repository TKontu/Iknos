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
