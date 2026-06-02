"""AGE openCypher helpers.

AGE quirks driving this module:
- Every Postgres session must `LOAD 'age'` and set search_path before any
  cypher() call (handled at the engine level in `session.py`).
- The Cypher body is opaque to SQL — parameters cannot be bound through
  cypher() the normal way. Callers must safely build the query text;
  never accept untrusted strings.
- Results come back as `agtype` columns; callers parse them.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.config import settings


async def bootstrap_session(session: AsyncSession) -> None:
    """Per-session AGE bootstrap.

    The engine-level event hook in `session.py` handles this for connections
    coming from the app's pool. Use this for sessions made outside the app
    (tests, scripts) where the hook is not registered.
    """
    await session.execute(text("LOAD 'age'"))
    await session.execute(text('SET search_path = ag_catalog, "$user", public'))


def cypher(query: str, returns: str = "result agtype") -> str:
    """Wrap a Cypher query body in the SQL/AGE invocation."""
    return f"SELECT * FROM cypher('{settings.graph_name}', $$ {query} $$) AS ({returns})"


async def execute_cypher(
    session: AsyncSession,
    query: str,
    returns: str = "result agtype",
) -> list[Any]:
    """Run a Cypher query, return the rows."""
    result = await session.execute(text(cypher(query, returns)))
    return list(result.all())
