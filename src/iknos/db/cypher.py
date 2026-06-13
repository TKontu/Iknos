"""The Cypher chokepoint (W8) — a thin, injection-safe query builder over :mod:`iknos.db.age`.

Before W8, ~50 call sites hand-assembled openCypher with f-strings, interpolating node labels,
edge types, ids, timestamps and enum values directly into the query text. That was *safe by
convention* — values came from UUIDs, ``isoformat()`` and ``StrEnum``s — but not *by
construction*: one future call site interpolating a user-influenced value would inject silently
(AGE cannot bind parameters into the Cypher body — see :mod:`iknos.db.age`). This module makes the
discipline construction-enforced and greppable:

- **Validated label/edge vocabularies.** :class:`NodeLabel` and :class:`EdgeType` enumerate every
  vertex label and edge type in the graph; the builder accepts *only* these enums where a label or
  edge type goes into the query text. A typo or an unknown label is a construction-time error, not a
  malformed query at runtime.
- **Mandatory value escaping.** Every interpolated *value* flows through :func:`lit` (scalars) or
  ``cypher_map`` (property maps) from :mod:`iknos.db.age` — the one place the Cypher-level escaping
  lives. The builder never inlines a bare value.
- **A clause builder, not a string.** :class:`CypherQuery` composes ``MATCH`` / ``OPTIONAL MATCH``
  / ``WHERE`` / ``WITH`` / ``CREATE`` / ``MERGE`` / ``SET`` / ``DETACH DELETE`` / ``RETURN`` /
  ``ORDER BY`` from safe fragments (:func:`node`, :func:`rel`). Call sites never write the
  structural keywords themselves, so the CI gate (``scripts``-exempt) can forbid raw f-string in
  ``src/`` outside this module and ``db/age.py``.

This is the *write/read* seam on top of ``db/age.py``'s *transport* seam (``execute_cypher`` wraps
the SQL/AGE invocation with the dollar-quote injection guard). The pure ``merge_vertex`` /
``merge_edge`` upsert primitives stay in ``db/age.py``; this module's :func:`merge_vertex` /
:func:`merge_edge` re-export them with the enum-typed label/edge signature.

The query-text assembly (:meth:`CypherQuery.render`, :func:`node`, :func:`rel`, :func:`lit`) is pure
and DB-free, so it unit-tests without an engine — exactly like ``cypher_map``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from iknos.db.age import (
    cypher_map,
    cypher_string_literal,
    execute_cypher,
)
from iknos.db.age import merge_edge as _merge_edge
from iknos.db.age import merge_vertex as _merge_vertex

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class NodeLabel(StrEnum):
    """Every vertex label in the AGE graph (§10). The builder accepts only these where a label is
    interpolated, so an unknown/typo'd label fails at construction, not as malformed Cypher."""

    BOX = "Box"
    DOCUMENT = "Document"
    SPAN = "Span"
    PROPOSITION = "Proposition"
    FACT = "Fact"
    ACTOR = "Actor"
    OBJECT = "Object"
    MENTION = "Mention"
    DEDUCTIVE_CONCLUSION = "DeductiveConclusion"
    INDUCTIVE_CONCLUSION = "InductiveConclusion"
    HYPOTHESIS = "Hypothesis"


class EdgeType(StrEnum):
    """Every edge type in the AGE graph (§10). Same validation role as :class:`NodeLabel` for the
    relationship type interpolated into a pattern."""

    INVOLVES = "INVOLVES"
    EVIDENCED_BY = "EVIDENCED_BY"
    SAME_AS = "SAME_AS"
    ANCHORS_TO = "ANCHORS_TO"
    REFERS_TO = "REFERS_TO"
    DIRECT_PART_OF = "directPartOf"
    PART_OF = "partOf"
    DERIVED_FROM = "DERIVED_FROM"
    MEMBER_OF = "MEMBER_OF"
    SUPPORTS = "SUPPORTS"
    REFUTES = "REFUTES"


def lit(value: Any) -> str:
    """A single Cypher scalar literal with mandatory escaping — the inline-value seam.

    The scalar counterpart to ``cypher_map`` (which renders a *map* literal): used for values that
    sit directly in a ``SET``/``WHERE``/``IN`` position rather than inside a property map. Mirrors
    ``cypher_map``'s per-type rules exactly so the two cannot diverge — strings (and ``StrEnum``,
    which *is* a str) are single-quote escaped via ``cypher_string_literal``; ``bool`` →
    ``true``/``false``; ``int``/``float`` inline bare; ``None`` → ``null``; anything else is
    JSON-encoded into an escaped string literal (the ``cypher_map`` fallback for list/dict values).
    """
    if isinstance(value, str):  # StrEnum is a str subclass — handled here, serializing to its value
        return cypher_string_literal(str(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    return cypher_string_literal(json.dumps(value))


def lit_list(values: Any) -> str:
    """A Cypher list literal of escaped scalars, e.g. ``['a', 'b']`` — for ``WHERE x IN [...]``."""
    return "[" + ", ".join(lit(v) for v in values) + "]"


def node(
    var: str = "",
    label: NodeLabel | None = None,
    props: dict[str, Any] | None = None,
) -> str:
    """A vertex pattern fragment ``(var:Label {k: 'v'})`` — label validated, props escaped.

    All three parts are optional: ``node("p")`` → ``(p)``; ``node("", props={"id": x})`` → an
    anonymous node with a filter; ``node("s", NodeLabel.SPAN, {"id": sid})`` → the full form. The
    ``props`` map is escaped through ``cypher_map`` (the same machinery the old f-strings used), so
    ids/values cannot inject.
    """
    lbl = f":{label.value}" if label is not None else ""
    body = f" {cypher_map(props)}" if props else ""
    return f"({var}{lbl}{body})"


def rel(
    etype: EdgeType | None = None,
    *,
    var: str = "",
    props: dict[str, Any] | None = None,
    directed: bool = True,
) -> str:
    """A relationship pattern fragment between two :func:`node` fragments.

    ``rel(EdgeType.INVOLVES)`` → ``-[:INVOLVES]->``; ``rel(EdgeType.SAME_AS, var="r")`` →
    ``-[r:SAME_AS]->``; ``rel(var="r", directed=False)`` → ``-[r]-`` (the untyped, undirected edge
    the degree check uses). The edge type is enum-validated; ``props`` (rare in patterns) is escaped
    through ``cypher_map``.
    """
    typ = f":{etype.value}" if etype is not None else ""
    body = f" {cypher_map(props)}" if props else ""
    inner = f"[{var}{typ}{body}]"
    return f"-{inner}->" if directed else f"-{inner}-"


class CypherQuery:
    """A composable openCypher query. Each method appends one clause from already-safe fragments.

    Clause methods return ``self`` for chaining. ``MATCH``/``OPTIONAL MATCH``/``CREATE``/``MERGE``
    take a *pattern* built from :func:`node`/:func:`rel`; ``WHERE`` takes one or more condition
    expressions (joined with ``AND``); ``SET`` takes one or more ``prop = <lit>`` assignments;
    ``WITH``/``RETURN``/``ORDER BY`` take projection expressions. Condition/projection/assignment
    expressions reference variables, properties and functions (``count(r)``, ``properties(n)``) —
    they carry no untrusted value (those go through :func:`lit`) and no structural keyword, which is
    what lets the CI gate forbid those keywords in raw f-strings elsewhere.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []

    def match(self, pattern: str) -> CypherQuery:
        self._parts.append(f"MATCH {pattern}")
        return self

    def optional_match(self, pattern: str) -> CypherQuery:
        self._parts.append(f"OPTIONAL MATCH {pattern}")
        return self

    def where(self, *conditions: str) -> CypherQuery:
        self._parts.append("WHERE " + " AND ".join(conditions))
        return self

    def with_(self, expression: str) -> CypherQuery:
        self._parts.append(f"WITH {expression}")
        return self

    def create(self, pattern: str) -> CypherQuery:
        self._parts.append(f"CREATE {pattern}")
        return self

    def merge(self, pattern: str) -> CypherQuery:
        self._parts.append(f"MERGE {pattern}")
        return self

    def set(self, *assignments: str) -> CypherQuery:
        self._parts.append("SET " + ", ".join(assignments))
        return self

    def detach_delete(self, *variables: str) -> CypherQuery:
        self._parts.append("DETACH DELETE " + ", ".join(variables))
        return self

    def return_(self, expression: str) -> CypherQuery:
        self._parts.append(f"RETURN {expression}")
        return self

    def order_by(self, expression: str) -> CypherQuery:
        self._parts.append(f"ORDER BY {expression}")
        return self

    def render(self) -> str:
        """The assembled Cypher query body (one space between clauses)."""
        return " ".join(self._parts)

    async def run(self, session: AsyncSession, returns: str = "result agtype") -> list[Any]:
        """Execute through :func:`iknos.db.age.execute_cypher` and return the rows."""
        return await execute_cypher(session, self.render(), returns)


async def merge_vertex(session: AsyncSession, label: NodeLabel, props: dict[str, Any]) -> None:
    """Enum-typed wrapper over :func:`iknos.db.age.merge_vertex` (upsert a vertex on ``id``)."""
    await _merge_vertex(session, label.value, props)


async def merge_edge(
    session: AsyncSession,
    *,
    src_id: Any,
    dst_id: Any,
    label: EdgeType,
    props: dict[str, Any],
) -> None:
    """Enum-typed wrapper over :func:`iknos.db.age.merge_edge` (upsert one edge between two ids)."""
    await _merge_edge(session, src_id=src_id, dst_id=dst_id, label=label.value, props=props)
