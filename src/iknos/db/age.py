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

# Note: ``iknos.config.settings`` (which requires DATABASE_URL) is imported lazily inside
# ``cypher()`` — the only function here that needs it — so the pure serialization helpers
# (``cypher_map``, ``merge_*``, ``parse_agtype_map``) import DB-free, exactly like core/ingest.py
# and core/proposition.py keep their pure logic importable without an env (G1.17 R7 unit tests of
# ``cypher_map`` rely on this).


def cypher_string_literal(value: str) -> str:
    """A single-quoted Cypher string literal with the Cypher-level escaping.

    The one place the Cypher string escaping lives — backslash first, then single-quote
    (order matters) — so the map serializer (:func:`cypher_map`) and any per-property ``SET``
    that inlines a value cannot diverge on it. Values only, never identifiers/labels (see the
    module docstring); the SQL-level dollar-quoting in :func:`cypher` is the second layer.
    """
    esc = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{esc}'"


def cypher_map(props: dict[str, Any]) -> str:
    """Serialize a dict into a Cypher map literal, e.g. ``{id: 'abc', n: 3}``.

    AGE's cypher() cannot bind parameters into the Cypher body (see module
    docstring), so values must be inlined into the query text. Strings are
    single-quote escaped; never pass untrusted keys (only values are escaped).
    """
    parts: list[str] = []
    for k, v in props.items():
        if isinstance(v, str):
            parts.append(f"{k}: {cypher_string_literal(v)}")
        elif isinstance(v, bool):
            parts.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
        elif v is None:
            parts.append(f"{k}: null")
        else:
            parts.append(f"{k}: {cypher_string_literal(json.dumps(v))}")
    return "{" + ", ".join(parts) + "}"


async def bootstrap_session(session: AsyncSession) -> None:
    """Per-session AGE bootstrap.

    The engine-level event hook in `session.py` handles this for connections
    coming from the app's pool. Use this for sessions made outside the app
    (tests, scripts) where the hook is not registered.
    """
    await session.execute(text("LOAD 'age'"))
    await session.execute(text('SET search_path = ag_catalog, "$user", public'))


def _dollar_quote_tag(body: str) -> str:
    """A PostgreSQL dollar-quote tag (``$iknos$`` / ``$iknos1$`` / …) absent from ``body``.

    The Cypher body is wrapped in a dollar-quoted SQL string so its single-quoted Cypher literals
    need no SQL-level escaping. A fixed ``$$`` delimiter is unsafe: a property **value** carrying
    ``$$`` (LaTeX math, ``$$`` in document text or LLM output — values reach here via
    :func:`cypher_map`) would close the quote early and inject raw SQL (G1.17 R7 — caught by the
    cypher_map fuzz round-trip). Pick the shortest ``$iknosN$`` tag that does not occur in the
    body, so no body content can terminate it. ``cypher_map`` escaping handles the Cypher level;
    this handles the SQL level — the two-layer boundary the module docstring warns about.
    """
    n = 0
    while True:
        tag = f"$iknos{n or ''}$"
        if tag not in body:
            return tag
        n += 1


def _build_cypher_sql(graph_name: str, query: str, returns: str) -> str:
    """Assemble the ``SELECT * FROM cypher(...)`` invocation — the pure, config-free seam.

    Splitting this out of :func:`cypher` keeps the SQL/AGE statement-assembly (the two-layer
    injection boundary) unit-testable without importing the config singleton, which the rest
    of the unit suite deliberately never does. The Cypher ``query`` body is wrapped in a
    dollar-quoted SQL string using a tag absent from the body (:func:`_dollar_quote_tag`), so
    no body content — including a value carrying ``$$`` (see that function) — can terminate
    the quote early and inject SQL. ``graph_name`` is operator config validated at its source
    (``config.Settings.graph_name``), not untrusted input, so it is interpolated directly.
    """
    tag = _dollar_quote_tag(query)
    return f"SELECT * FROM cypher('{graph_name}', {tag} {query} {tag}) AS ({returns})"


def cypher(query: str, returns: str = "result agtype") -> str:
    """Wrap a Cypher query body in the SQL/AGE invocation (injection-safe dollar quoting)."""
    from iknos.config import settings

    return _build_cypher_sql(settings.graph_name, query, returns)


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
