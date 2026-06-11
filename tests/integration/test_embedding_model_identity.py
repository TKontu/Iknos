"""G1.16 embedding-model identity — live Postgres+AGE.

Three properties, all silent-wrong before this lands:

1. The span write path refuses a model swap (``persist_spans`` → EmbeddingModelMismatchError)
   instead of mixing two ANN spaces in ``document_embeddings``.
2. The proposition write path refuses one too (``propositionize_document``) — the load-bearing
   case, since the extraction cache key keys on the *LLM* model, not the embedding model, so a
   substrate swap would otherwise slip straight past StaleExtractionError.
3. ``scripts``' reindex core (``core/reembed.py``) converges every dense row to the target model
   and is re-runnable (a second pass is a 0/0 no-op).

LLM + embedding substrate are mocked (no vLLM / model download); the reembed substrate is a
deterministic fake supplying fixed vectors, so the test asserts the *plumbing* (which rows move,
idempotency), not model numerics.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.embeddings import EmbeddingModelMismatchError
from iknos.core.ingest import persist_spans, span_content_hash
from iknos.core.proposition import Propositionizer
from iknos.core.reembed import reembed_to_model
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.db.orm import DocumentEmbedding, PropositionEmbedding

pytestmark = pytest.mark.asyncio

_PARAMS = {"max_len": 10, "penalty_weight": 0.1, "density_weight": 0.5}
_VEC = [0.05] * 1024


async def _seed_document(session: AsyncSession, raw: str) -> uuid.UUID:
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": raw},
    )
    return doc_id


# --- 1. span path refuses a model swap ---


async def test_persist_spans_refuses_embedding_model_swap(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "First sentence here. Second follows."
    doc_id = await _seed_document(session, raw)
    char_spans = [(0, 20), (21, len(raw))]

    await persist_spans(
        session,
        doc_id,
        char_spans,
        [_VEC, _VEC],
        content_hash=span_content_hash(raw, segmenter_params=_PARAMS, model="model-a"),
        segmenter_params=_PARAMS,
        model="model-a",
    )
    await session.commit()

    # Same document, a different embedding model → cosine across spaces is meaningless; refuse it.
    with pytest.raises(EmbeddingModelMismatchError, match="reembed"):
        await persist_spans(
            session,
            doc_id,
            char_spans,
            [_VEC, _VEC],
            content_hash=span_content_hash(raw, segmenter_params=_PARAMS, model="model-b"),
            segmenter_params=_PARAMS,
            model="model-b",
        )
    await session.rollback()

    # Nothing mixed in: every existing row is still model-a.
    models = (
        (
            await session.execute(
                text("SELECT DISTINCT model FROM document_embeddings WHERE document_id = :d"),
                {"d": doc_id},
            )
        )
        .scalars()
        .all()
    )
    assert models == ["model-a"]


# --- 2. proposition path refuses a model swap ---


def _propositionizer(embed_model: str) -> Propositionizer:
    llm = MagicMock()
    llm.model = "extractor-model"  # same extractor across both runs — only the substrate swaps
    llm.guided_complete = AsyncMock(
        return_value={"propositions": [{"text": "The bearing failed under load."}]}
    )
    substrate = MagicMock()
    substrate.model_name = embed_model
    substrate.embed_passages = MagicMock(return_value=[[0.1] * 1024])
    return Propositionizer(llm, substrate, context_window=8, concurrency=4)


async def _seed_one_span(session: AsyncSession, doc_id: uuid.UUID, raw: str) -> list:
    from iknos.types.nodes import Span

    span_id = uuid.uuid5(doc_id, "0:1:0")
    await execute_cypher(
        session,
        f"MERGE (s:Span {cypher_map({'id': str(span_id), 'document_id': str(doc_id)})})",
    )
    await session.commit()
    return [Span(id=span_id, document_id=doc_id, start=0, end=len(raw), level=0)]


async def test_propositionize_refuses_embedding_model_swap(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "The bearing failed under load."
    doc_id = await _seed_document(session, raw)
    spans = await _seed_one_span(session, doc_id, raw)

    # First run on model-a writes proposition_embeddings under model-a.
    await _propositionizer("embed-a").propositionize_document(session, doc_id, spans, raw)

    # A substrate swap keeps the extractor identical, so the content-hash idempotency would NOT
    # catch it — only the dedicated embedding-model guard does.
    with pytest.raises(EmbeddingModelMismatchError, match="reembed"):
        await _propositionizer("embed-b").propositionize_document(session, doc_id, spans, raw)
    await session.rollback()

    models = (
        (
            await session.execute(
                text("SELECT DISTINCT model FROM proposition_embeddings WHERE document_id = :d"),
                {"d": doc_id},
            )
        )
        .scalars()
        .all()
    )
    assert models == ["embed-a"]


# --- 3. reembed converges to the target model and is re-runnable ---


class _FakeContext:
    """Stands in for a DocumentContext: every span pools to the same marker vector."""

    def pool_span(self, start_char: int, end_char: int) -> list[float]:
        return [0.5] * 1024


class _FakeSubstrate:
    """Deterministic stand-in for EmbeddingSubstrate — distinct markers for spans vs passages."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def embed_document(self, text: str) -> _FakeContext:
        return _FakeContext()

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] * 1024 for _ in texts]


async def test_reembed_converges_and_is_rerunnable(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "hello world, this is the source text."
    doc_id = await _seed_document(session, raw)

    # A span dense row and a proposition dense row, both on the OLD model.
    span_id = uuid.uuid4()
    session.add(
        DocumentEmbedding(
            document_id=doc_id,
            span_id=span_id,
            span_start=0,
            span_end=11,
            level=0,
            embedding=[0.1] * 1024,
            model="old-model",
        )
    )
    prop_id = uuid.uuid4()
    await execute_cypher(
        session,
        f"CREATE (p:Proposition {cypher_map({'id': str(prop_id), 'text': 'Hello world.'})}) "
        "RETURN p",
        returns="p agtype",
    )
    session.add(
        PropositionEmbedding(
            proposition_id=prop_id,
            document_id=doc_id,
            embedding=[0.1] * 1024,
            model="old-model",
        )
    )
    await session.commit()

    report = await reembed_to_model(session, _FakeSubstrate("new-model"), batch_size=128)
    assert (report.span_rows, report.proposition_rows) == (1, 1)

    # Both rows converged to the target model, and the vectors were actually re-pooled/re-embedded
    # (markers 0.5 for spans, 0.25 for passages — not the seeded 0.1).
    span_model, span_vec = (
        await session.execute(
            text("SELECT model, embedding::text FROM document_embeddings WHERE span_id = :s"),
            {"s": span_id},
        )
    ).one()
    assert span_model == "new-model" and span_vec.startswith("[0.5")

    prop_model, prop_vec = (
        await session.execute(
            text(
                "SELECT model, embedding::text FROM proposition_embeddings "
                "WHERE proposition_id = :p"
            ),
            {"p": prop_id},
        )
    ).one()
    assert prop_model == "new-model" and prop_vec.startswith("[0.25")

    # Re-runnable: a second pass finds nothing off-target → a 0/0 no-op.
    again = await reembed_to_model(session, _FakeSubstrate("new-model"), batch_size=128)
    assert (again.span_rows, again.proposition_rows) == (0, 0)
