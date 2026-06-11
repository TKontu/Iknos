"""cypher_map → live AGE → read-back round-trip over an adversarial corpus (G1.17 R7).

The unit suite (``tests/unit/test_cypher_map.py``) property-fuzzes the escaping *logic*; this
asserts the escaping also matches AGE's own Cypher string grammar — that a hostile value written
through ``cypher_map`` and read back via ``properties(n)`` survives byte-for-byte through a real
AGE engine. Document text and LLM output cross this hand-rolled boundary, so an escaping mismatch
here is a write-corruption / injection bug.

A curated corpus rather than ``hypothesis`` here on purpose: ``st.text()`` readily emits NUL and
lone surrogates, which **Postgres** rejects (a storage-layer limit, not a ``cypher_map`` defect),
and ``@given`` does not compose with the function-scoped async ``session`` fixture. The corpus
concentrates the metacharacters that actually exercise the boundary.
"""

import uuid

import pytest

from iknos.db.age import bootstrap_session, cypher_map, execute_cypher, parse_agtype_map

pytestmark = pytest.mark.asyncio

# Hostile values: quotes, backslashes (incl. runs that interact with quote-escaping), Cypher/agtype
# punctuation, injection attempts, newlines/tabs, and unicode. NUL is excluded — Postgres text
# cannot store it, so it is a DB limit rather than an escaping concern.
_HOSTILE = [
    "'",
    "\\",
    "\\'",
    "''",
    "\\\\",
    "\\\\'",
    "\\\\\\'",
    'a"b',
    "abc\\",  # trailing backslash — must not escape the closing quote
    "'; MATCH (x) DETACH DELETE x //",
    "' RETURN 1 AS injected //",
    "$$ break out $$",
    "{id: 'spoof'}",
    '{"json": [1, 2, 3]}',
    "[1, 2, 3]",
    "}{",
    "line1\nline2\ttabbed",
    "café ☃ 𝕏 — ünïcödé",
    "mixed '\\' \" {} [] :, fragment",
]


async def test_cypher_map_round_trips_hostile_strings_through_age(session) -> None:
    await bootstrap_session(session)
    label = "FuzzNode"

    for value in _HOSTILE:
        node_id = str(uuid.uuid4())
        props = {"id": node_id, "val": value}
        await execute_cypher(session, f"CREATE (n:{label} {cypher_map(props)})")
        rows = await execute_cypher(
            session,
            f"MATCH (n:{label} {cypher_map({'id': node_id})}) RETURN properties(n)",
            returns="props agtype",
        )
        assert len(rows) == 1, f"value {value!r} did not round-trip to exactly one node"
        readback = parse_agtype_map(rows[0][0])
        assert readback["val"] == value, f"value {value!r} corrupted to {readback['val']!r}"
        assert readback["id"] == node_id

    await session.rollback()
