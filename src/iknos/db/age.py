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


# --- shared write/read primitives (idempotent MERGE-on-id; agtype parsing) ---


async def merge_vertex(session: AsyncSession, label: str, props: dict[str, Any]) -> None:
    """``MERGE (n:Label {id}) SET n = {...}`` — upsert a vertex keyed on ``id``.

    The single MERGE-on-id implementation reused by every writer (the box registry,
    the domain-pack loader, later operators) so the upsert discipline cannot diverge
    across call sites. ``SET n = {...}`` is full-replace: callers that must preserve a
    create-only field (e.g. bitemporal ``valid_from``) read-first and skip the write
    when the vertex already exists rather than re-issuing it (the G0.R1 discipline).
    ``props`` must carry ``id``; only values are escaped, never keys/labels.
    """
    body = cypher_map(props)
    await execute_cypher(
        session,
        f"MERGE (n:{label} {{id: '{props['id']}'}}) SET n = {body}",
    )


async def merge_edge(
    session: AsyncSession,
    *,
    src_id: Any,
    dst_id: Any,
    label: str,
    props: dict[str, Any],
) -> None:
    """MERGE one edge of ``label`` between two id-identified vertices, then set props.

    Merges on endpoints + label (not on properties), so it is the correct idempotent
    key only when at most one edge of ``label`` exists per (src, dst) pair — the
    caller's invariant. Both endpoints must already exist (MATCH), or the MERGE
    silently no-ops.
    """
    body = cypher_map(props)
    await execute_cypher(
        session,
        f"MATCH (a {{id: '{src_id}'}}), (b {{id: '{dst_id}'}}) "
        f"MERGE (a)-[r:{label}]->(b) SET r = {body}",
    )


def unquote_agtype(v: Any) -> str:
    """AGE returns agtype strings double-quoted (``\"foo\"``); strip to plain str."""
    s = str(v)
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def parse_agtype_map(v: Any) -> dict[str, Any]:
    """Parse an agtype map (e.g. from ``RETURN properties(n)``) into a Python dict.

    AGE renders a property map as JSON text; the driver hands it back as a string
    (or, defensively, an already-parsed dict). List/dict-valued properties were
    JSON-encoded into string properties by :func:`cypher_map`, so they come back as
    JSON strings — readers decode those a second time (see the box ``*_from_props``).
    """
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    return json.loads(str(v))  # type: ignore[no-any-return]
