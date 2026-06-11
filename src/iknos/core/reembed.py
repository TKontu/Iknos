"""Re-embed every dense row to a target embedding model — the G1.16 reindex path (core).

Once embedding rows carry their producing ``model`` (migration 0008), the ingest guards
(:class:`~iknos.core.embeddings.EmbeddingModelMismatchError`) refuse to *mix* two embedding
spaces in place. This module is the sanctioned way to actually *migrate* to a new model: it
re-embeds every ``document_embeddings`` span vector (via late-chunking ``embed_document`` +
``pool_span`` over the document's stored text) and every ``proposition_embeddings`` vector
(via ``embed_passages`` over the proposition text read back from AGE), stamping each row with
the target ``model``. After it converges, ingest of the same documents under the new model is
a clean no-op rather than a mismatch error.

Properties:

- **Idempotent / re-runnable.** Only rows whose ``model`` differs from the target are touched;
  a second run is a no-op. A crashed run resumes — each batch commits, so completed rows are
  already on the target model and skipped next time.
- **Batched, per-batch transactions.** Span rows commit per document; proposition rows commit
  per ``batch_size`` group — bounded memory and durable progress over a large corpus.
- **Fails loud, never silently mixes.** A target model with a different vector dimension is
  rejected by the pgvector column type. A document longer than the model context raises
  :class:`~iknos.core.embeddings.DocumentTooLongError` (G1.13) rather than re-embedding a
  truncated prefix.

The substrate is taken by injection (a :class:`_Substrate`) so the target model is named once
at the call site and an integration test can supply a deterministic stand-in instead of
downloading a model. ``scripts/reembed.py`` is the CLI that wires the real substrate + engine.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.embeddings import DocumentContext
from iknos.db.age import execute_cypher, unquote_agtype
from iknos.db.orm import DocumentEmbedding, PropositionEmbedding

logger = logging.getLogger(__name__)


class _Substrate(Protocol):
    """The slice of :class:`~iknos.core.embeddings.EmbeddingSubstrate` reembed needs."""

    model_name: str

    def embed_document(self, text: str) -> DocumentContext: ...

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class ReembedReport:
    """Counts of rows moved onto the target model (0/0 on a fully-converged re-run)."""

    target_model: str
    span_rows: int
    proposition_rows: int


async def reembed_to_model(
    session: AsyncSession, substrate: _Substrate, *, batch_size: int = 128
) -> ReembedReport:
    """Re-embed all off-target dense rows to ``substrate.model_name``. See module docstring.

    Commits per batch (per document for spans, per ``batch_size`` for propositions), so the
    caller does **not** wrap this in one transaction — partial progress is durable and resumable.
    Requires an AGE-bootstrapped session (proposition text is read from the graph).
    """
    target = substrate.model_name
    span_rows = await _reembed_spans(session, substrate, target)
    proposition_rows = await _reembed_propositions(session, substrate, target, batch_size)
    report = ReembedReport(target, span_rows, proposition_rows)
    logger.info(
        "reembed → %s: %d span rows, %d proposition rows updated",
        target,
        span_rows,
        proposition_rows,
    )
    return report


async def _reembed_spans(session: AsyncSession, substrate: _Substrate, target: str) -> int:
    """Re-pool every off-target span vector from its document's stored text, one doc per commit."""
    doc_ids = (
        (
            await session.execute(
                text("SELECT DISTINCT document_id FROM document_embeddings WHERE model <> :m"),
                {"m": target},
            )
        )
        .scalars()
        .all()
    )

    updated = 0
    for doc_id in doc_ids:
        raw = (
            await session.execute(
                text("SELECT raw_text FROM document_content WHERE document_id = :d"),
                {"d": doc_id},
            )
        ).scalar_one_or_none()
        if raw is None:
            # An embedding row without its source text cannot be re-pooled; skip loudly and leave
            # it off-target so the run does not report false convergence.
            logger.warning("document %s has dense rows but no document_content; skipping", doc_id)
            continue

        # One late-chunking pass; every span pools from it (DocumentTooLongError surfaces here).
        context = substrate.embed_document(raw)
        rows = (
            await session.execute(
                text(
                    "SELECT id, span_start, span_end FROM document_embeddings "
                    "WHERE document_id = :d AND model <> :m"
                ),
                {"d": doc_id, "m": target},
            )
        ).all()
        for row_id, span_start, span_end in rows:
            vector = context.pool_span(span_start, span_end)
            # ORM update so the pgvector column type adapts the list → vector (a raw text() bind
            # would lose it); a dimension mismatch under the new model is rejected here.
            await session.execute(
                update(DocumentEmbedding)
                .where(DocumentEmbedding.id == row_id)
                .values(embedding=vector, model=target)
            )
            updated += 1
        await session.commit()
    return updated


async def _reembed_propositions(
    session: AsyncSession, substrate: _Substrate, target: str, batch_size: int
) -> int:
    """Re-embed every off-target proposition vector from its AGE text, batch_size per commit."""
    prop_ids = (
        (
            await session.execute(
                text(
                    "SELECT proposition_id FROM proposition_embeddings "
                    "WHERE model <> :m ORDER BY proposition_id"
                ),
                {"m": target},
            )
        )
        .scalars()
        .all()
    )

    updated = 0
    for start in range(0, len(prop_ids), batch_size):
        batch = list(prop_ids[start : start + batch_size])
        texts = await _proposition_texts(session, batch)
        # Preserve batch order; drop any proposition whose vertex/text is missing (logged below).
        items = [(pid, texts[pid]) for pid in batch if pid in texts]
        missing = len(batch) - len(items)
        if missing:
            logger.warning(
                "%d proposition(s) have a dense row but no graph text; skipping", missing
            )
        if not items:
            continue

        vectors = substrate.embed_passages([t for _, t in items])
        for (pid, _), vector in zip(items, vectors, strict=True):
            await session.execute(
                update(PropositionEmbedding)
                .where(PropositionEmbedding.proposition_id == pid)
                .values(embedding=vector, model=target)
            )
            updated += 1
        await session.commit()
    return updated


async def _proposition_texts(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Read ``{proposition_id: text}`` from AGE for a batch of ids (one Cypher round-trip).

    Proposition text lives on the graph vertex, not in ``proposition_embeddings``; reading it
    back here keeps the dense table free of duplicated text. Ids are UUIDs (no escaping needed).
    """
    if not ids:
        return {}
    id_list = ", ".join(f"'{i}'" for i in ids)
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition) WHERE p.id IN [{id_list}] RETURN p.id, p.text",
        returns="id agtype, txt agtype",
    )
    return {uuid.UUID(unquote_agtype(rid)): unquote_agtype(rtxt) for rid, rtxt in rows}
