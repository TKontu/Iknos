"""Plain-RAG baseline (Trial A0 / V4) — fixed-size chunks, top-k cosine retrieval, one answer call.

A *fair strong* RAG rig for the E1 go/no-go (architecture.md §8): the same LLM endpoint and
embedding substrate the system uses, but what a competent team would build **without** this
project — naive fixed-size chunking (``baselines/chunking.py``), a top-k cosine retrieval over an
own ``baseline_chunks`` index, and a single grounded answer call. No iknos segmentation,
propositions, graph, candidate generation, adjudication, or QBAF (enforced by
``tests/unit/test_baselines_import_boundary.py``). It emits the shared
:class:`~iknos.baselines.contract.BaselineAnswer` so the V3 harness scores it identically to the
other rungs.

The orchestrator depends on three seams as Protocols so it is unit-testable with fakes (no model,
no DB): a :class:`PassageEmbedder` (the embedding substrate), a :class:`GuidedLLM` (the LLM
client), and an async session factory. The pure pieces — prompt assembly and answer parsing — are
module functions tested directly with a mock LLM.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.baselines.chunking import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    Chunk,
    PassageTokenizer,
    chunk_document,
)
from iknos.baselines.contract import BaselineAnswer, BaselineQuestion
from iknos.db.orm import BaselineChunk

DEFAULT_TOP_K = 8

# Pin greedy decoding by default (V12). Every other LLM consumer in the project pins
# ``{"temperature": 0.0}`` (e.g. ``core/extract.py``); leaving the baselines at the server's
# default made E1 answers and confidences vary run to run — an unfair, unreproducible instrument.
# The regime is a constructor/CLI knob and is recorded in the answers-file ``meta``.
DEFAULT_SAMPLING: dict[str, Any] = {"temperature": 0.0}

# A fixed namespace so a corpus document id (e.g. "d01") maps to a stable baseline document
# UUID — re-ingesting the same document is idempotent and citations are reproducible across runs.
_BASELINE_DOC_NAMESPACE = uuid.UUID("ba5e1000-0000-0000-0000-000000000001")


def baseline_document_uuid(document_id: str) -> uuid.UUID:
    """Deterministic baseline UUID for a corpus document id (uuid5 — stable, idempotent)."""
    return uuid.uuid5(_BASELINE_DOC_NAMESPACE, document_id)


# The guided-decode schema for the single answer call: a grounded answer, the excerpt numbers it
# relied on, and a verbalized confidence. additionalProperties is closed so the model cannot pad.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer_text": {"type": "string"},
        "cited_chunks": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "The 1-based numbers of the excerpts the answer relies on.",
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["answer_text", "cited_chunks", "confidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a careful question-answering assistant. You are given a QUESTION and a numbered "
    "list of EXCERPTS retrieved from a document corpus. Answer the question using ONLY the "
    "information in the excerpts; do not use outside knowledge. If the excerpts do not contain "
    "the answer, say so plainly. Cite the excerpt numbers you relied on, and give a calibrated "
    "confidence in [0, 1] reflecting how well the excerpts support your answer."
)


class PassageEmbedder(Protocol):
    """The embedding seam: embed short passages to one vector each (``EmbeddingSubstrate``)."""

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...


class GuidedLLM(Protocol):
    """The LLM seam: a guided-JSON chat completion (``core.llm.LLMClient.guided_complete``)."""

    async def guided_complete(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        sampling: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk returned by retrieval, with its cosine distance to the query (smaller = closer)."""

    id: str
    document_id: str
    chunk_index: int
    char_start: int
    char_end: int
    text: str
    distance: float


def build_answer_messages(question: str, chunks: Sequence[RetrievedChunk]) -> list[dict[str, str]]:
    """Assemble the single answer call's messages from the question and the retrieved excerpts.

    Excerpts are numbered 1..k in retrieval order; the model cites by these numbers, which
    :func:`parse_answer` maps back to chunk ids. Pure — tested directly with a mock LLM.
    """
    excerpt_lines = []
    for i, chunk in enumerate(chunks, start=1):
        excerpt_lines.append(f"[{i}] {chunk.text.strip()}")
    excerpts = "\n\n".join(excerpt_lines) if excerpt_lines else "(no excerpts retrieved)"
    user = f"QUESTION:\n{question}\n\nEXCERPTS:\n{excerpts}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_answer(
    question_id: str,
    raw: dict[str, Any],
    presented: Sequence[RetrievedChunk],
) -> BaselineAnswer:
    """Turn the guided-JSON response into a :class:`BaselineAnswer`, mapping cited numbers to ids.

    Citation numbers are 1-based into ``presented``; out-of-range numbers are dropped (the model
    occasionally cites a number that was not shown) and duplicates collapsed, order preserved.
    Confidence is clamped to [0, 1] defensively even though the schema constrains it.
    """
    cited_ids: list[str] = []
    seen: set[str] = set()
    for n in raw.get("cited_chunks", []):
        idx = int(n) - 1
        if 0 <= idx < len(presented):
            chunk_id = presented[idx].id
            if chunk_id not in seen:
                seen.add(chunk_id)
                cited_ids.append(chunk_id)
    confidence = float(raw.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    return BaselineAnswer(
        question_id=question_id,
        answer_text=str(raw.get("answer_text", "")),
        cited_chunk_ids=tuple(cited_ids),
        confidence=confidence,
    )


class RagBaseline:
    """Orchestrates ingest → retrieve → answer for the plain-RAG rung.

    Tuning knobs (``top_k``, ``chunk_tokens``, ``overlap_tokens``) are constructor params, so the
    rig can be retrieval-tuned without code changes — a fair baseline is tuned, not a strawman.
    Stateless except for its injected seams; one instance answers any number of questions.
    """

    def __init__(
        self,
        *,
        embedder: PassageEmbedder,
        llm: GuidedLLM,
        session_factory: SessionFactory,
        tokenizer: PassageTokenizer,
        model_name: str,
        top_k: int = DEFAULT_TOP_K,
        chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
        sampling: dict[str, Any] | None = None,
    ) -> None:
        self._embedder = embedder
        self._llm = llm
        self._session_factory = session_factory
        self._tokenizer = tokenizer
        self._model_name = model_name
        self._top_k = top_k
        self._chunk_tokens = chunk_tokens
        self._overlap_tokens = overlap_tokens
        # Pin greedy by default (V12) so the rung is reproducible; an explicit dict overrides.
        self._sampling = DEFAULT_SAMPLING if sampling is None else sampling
        # The corpus this instance ingested — retrieval is scoped to it (V12). Two rigs over two
        # corpora in one DB must not retrieve each other's chunks (only filtering on `model` let
        # any previously-ingested corpus contaminate retrieval and get cited). Tracked in memory
        # rather than via a `corpus` column + migration: the E1 flow ingests then retrieves on one
        # instance, so the ingested-document set *is* the run's corpus and needs no schema change.
        self._ingested_doc_uuids: set[uuid.UUID] = set()

    def chunk(self, document_id: str, text: str) -> list[Chunk]:
        """Chunk a document with this rig's fixed-size policy (exposed for the runner/tests)."""
        return chunk_document(
            document_id,
            text,
            self._tokenizer,
            chunk_tokens=self._chunk_tokens,
            overlap_tokens=self._overlap_tokens,
        )

    async def ingest_document(self, document_id: str, text: str) -> int:
        """Chunk, embed, and upsert one document's chunks. Returns the number of chunks.

        Idempotent on ``(document_id, chunk_index, model)`` — re-ingesting overwrites rather than
        duplicating — so a re-run after a corpus edit is safe **including an edit that shrinks the
        document**: the upsert refreshes chunks ``0..n-1`` and a follow-up delete removes any tail
        chunks (index ``>= n``) a previous, longer version left behind, which retrieval would
        otherwise still surface and cite. The document is also registered for retrieval scoping.
        """
        chunks = self.chunk(document_id, text)
        doc_uuid = baseline_document_uuid(document_id)
        self._ingested_doc_uuids.add(doc_uuid)
        if not chunks:
            # An emptied document: drop any chunks a prior, non-empty version left behind.
            async with self._session_factory() as session:
                await session.execute(
                    delete(BaselineChunk).where(
                        BaselineChunk.document_id == doc_uuid,
                        BaselineChunk.model == self._model_name,
                    )
                )
                await session.commit()
            return 0
        vectors = self._embedder.embed_passages([c.text for c in chunks])
        rows = [
            {
                "document_id": doc_uuid,
                "chunk_index": c.index,
                "char_start": c.char_start,
                "char_end": c.char_end,
                "text": c.text,
                "embedding": vec,
                "model": self._model_name,
            }
            for c, vec in zip(chunks, vectors, strict=True)
        ]
        stmt = pg_insert(BaselineChunk).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["document_id", "chunk_index", "model"],
            set_={
                "char_start": stmt.excluded.char_start,
                "char_end": stmt.excluded.char_end,
                "text": stmt.excluded.text,
                "embedding": stmt.excluded.embedding,
            },
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            # Remove tail chunks from a previously longer version of this document (same model) so a
            # shrinking re-ingest cannot leave orphaned, still-retrievable evidence behind.
            await session.execute(
                delete(BaselineChunk).where(
                    BaselineChunk.document_id == doc_uuid,
                    BaselineChunk.model == self._model_name,
                    BaselineChunk.chunk_index >= len(rows),
                )
            )
            await session.commit()
        return len(rows)

    async def retrieve(self, question: str) -> list[RetrievedChunk]:
        """Top-k chunks by cosine distance to the question embedding (pgvector ``<=>``, HNSW).

        Scoped to the documents this rig ingested (``model`` is only the G1.16 vector-space guard,
        not a corpus boundary), so a second corpus ingested into the same ``baseline_chunks`` table
        cannot contaminate this run's retrieval. With nothing ingested, retrieval is empty.
        """
        if not self._ingested_doc_uuids:
            return []
        query_vec = self._embedder.embed_passages([question])[0]
        distance = BaselineChunk.embedding.cosine_distance(query_vec).label("distance")
        stmt = (
            select(
                BaselineChunk.id,
                BaselineChunk.document_id,
                BaselineChunk.chunk_index,
                BaselineChunk.char_start,
                BaselineChunk.char_end,
                BaselineChunk.text,
                distance,
            )
            .where(
                BaselineChunk.model == self._model_name,
                BaselineChunk.document_id.in_(self._ingested_doc_uuids),
            )
            .order_by(distance)
            .limit(self._top_k)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.all()
        return [
            RetrievedChunk(
                id=str(row.id),
                document_id=str(row.document_id),
                chunk_index=row.chunk_index,
                char_start=row.char_start,
                char_end=row.char_end,
                text=row.text,
                distance=float(row.distance),
            )
            for row in rows
        ]

    async def answer(self, question: BaselineQuestion) -> BaselineAnswer:
        """Retrieve and answer one question (one LLM call). The V4 rung is non-agentic."""
        chunks = await self.retrieve(question.text)
        messages = build_answer_messages(question.text, chunks)
        raw = await self._llm.guided_complete(messages, ANSWER_SCHEMA, self._sampling)
        return parse_answer(question.id, raw, chunks)
