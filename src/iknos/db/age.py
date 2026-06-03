"""AGE openCypher helpers.

AGE quirks driving this module:
- Every Postgres session must `LOAD 'age'` and set search_path before any
  cypher() call (handled at the engine level in `session.py`).
- The Cypher body is opaque to SQL — parameters cannot be bound through
  cypher() the normal way. Callers must safely build the query text;
  never accept untrusted strings.
- Results come back as `agtype` columns; callers parse them.
"""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.config import settings


def cypher_map(props: dict[str, Any]) -> str:
    """Serialize a dict into a Cypher map literal, e.g. ``{id: 'abc', n: 3}``.

    AGE's cypher() cannot bind parameters into the Cypher body (see module
    docstring), so values must be inlined into the query text. Strings are
    single-quote escaped; never pass untrusted keys (only values are escaped).
    """
    parts: list[str] = []
    for k, v in props.items():
        if isinstance(v, str):
            esc = v.replace("\\", "\\\\").replace("'", "\\'")
            parts.append(f"{k}: '{esc}'")
        elif isinstance(v, bool):
            parts.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
        elif v is None:
            parts.append(f"{k}: null")
        else:
            esc = json.dumps(v).replace("\\", "\\\\").replace("'", "\\'")
            parts.append(f"{k}: '{esc}'")
    return "{" + ", ".join(parts) + "}"


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
    """Run a Cypher query, return the rows.

    Uses ``exec_driver_sql`` rather than ``text()`` so the raw SQL goes straight
    to the driver. AGE's Cypher uses ``:Label`` / ``:TYPE`` syntax, which SQLAlchemy's
    ``text()`` would otherwise misparse as ``:name`` bind parameters. Binding into the
    Cypher body is impossible anyway (see module docstring), so raw execution is correct.
    """
    conn = await session.connection()
    result = await conn.exec_driver_sql(cypher(query, returns))
    return list(result.all())
