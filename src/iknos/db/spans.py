"""Span → text resolution as a local join (§10 resolution rule)."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_span_text(
    session: AsyncSession, document_id: uuid.UUID, start: int, end: int
) -> str | None:
    """Return the substring of the document's raw_text at [start, end)."""
    row = await session.execute(
        text(
            "SELECT substring(raw_text FROM :start_pos FOR :length) "
            "FROM document_content WHERE document_id = :doc_id"
        ),
        {"doc_id": document_id, "start_pos": start + 1, "length": end - start},
    )
    return row.scalar_one_or_none()
