"""Span → text resolution as a local join (§10 resolution rule)."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_span_text(
    session: AsyncSession, document_id: uuid.UUID, start: int, end: int
) -> str | None:
    """Return the substring of the document's raw_text at [start, end)."""
    # Use substr(text, int, int) rather than the SQL `substring(... FROM ... FOR ...)`
    # keyword form: the latter is type-ambiguous (it also has a regex overload), so
    # asyncpg infers the positional args as text and rejects the integer offsets.
    row = await session.execute(
        text(
            "SELECT substr(raw_text, :start_pos, :length) "
            "FROM document_content WHERE document_id = :doc_id"
        ),
        {"doc_id": document_id, "start_pos": start + 1, "length": end - start},
    )
    return row.scalar_one_or_none()
