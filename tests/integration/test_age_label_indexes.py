"""Verification test for migration 0007 — AGE label indexes are *used* (G0.R2).

Index existence is not index use. AGE wraps Cypher in SQL with sharp edges, and the
whole point of G0.R2 is that the index expression matches the filter the planner
actually emits: a property-map filter (``{id: ...}`` / ``{box: ...}``) compiles to the
agtype containment operator ``properties @> {...}``, which a **GIN on properties**
serves — a btree on ``agtype_access_operator(...)`` (the gap doc's first guess) would
exist and never be chosen. Edge traversal joins on the graphid ``start_id``/``end_id``
columns, served by **btree**. This test asserts, through the *real* ``cypher()`` call
path, that an index scan (not a seq scan of the label heap) backs each of those.

``enable_seqscan = off`` makes the assertion about *usability* deterministic: it forces
the planner to reveal whether a usable index exists for the filter, independent of how
many rows the shared graph happens to hold. If the index expression did not match the
filter, the planner would fall back to a (penalised) seq scan and the assertion fails —
which is exactly the regression this guards.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.age import bootstrap_session, cypher

pytestmark = pytest.mark.asyncio


async def _explain(session: AsyncSession, query: str, returns: str = "result agtype") -> dict:
    """EXPLAIN a Cypher query through the same ``cypher()`` wrapper the app runs.

    Returns the root plan node (a dict). ``enable_seqscan`` is disabled first so the
    plan reflects index *usability*, not the planner's row-count-dependent preference.
    """
    conn = await session.connection()
    await conn.exec_driver_sql("SET enable_seqscan = off")
    result = await conn.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {cypher(query, returns)}")
    raw = result.scalar_one()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    return plan[0]["Plan"]  # type: ignore[no-any-return]


def _walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _walk(child)


def _index_names(plan: dict[str, Any]) -> list[str]:
    """Index names used anywhere in the plan.

    Keyed off the ``Index Name`` field rather than ``Relation Name``: a
    ``Bitmap Index Scan`` node carries only ``Index Name`` (its ``Relation Name``
    lives on the parent ``Bitmap Heap Scan``), and a plain ``Index Scan`` carries
    both. The index name already encodes its label table by construction, so a name
    match is a sufficient and scan-shape-independent assertion that the right index
    backed the filter.
    """
    return [n["Index Name"] for n in _walk(plan) if n.get("Index Name")]


def _seq_scans_on(plan: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return [
        n
        for n in _walk(plan)
        if n.get("Node Type") == "Seq Scan" and n.get("Relation Name") == label
    ]


# (label, expected GIN index) — id-keyed MERGE/MATCH lookups (the per-writer hot path).
VERTEX_ID_CASES = [
    ("Span", "ix_span_props"),
    ("Proposition", "ix_proposition_props"),
    ("Object", "ix_object_props"),
    ("Box", "ix_box_props"),
]

# (label, expected GIN index) — box-scoped MATCH (Phase 2 box partitioning).
VERTEX_BOX_CASES = [
    ("Proposition", "ix_proposition_props"),
    ("Fact", "ix_fact_props"),
]


@pytest.mark.parametrize(("label", "index"), VERTEX_ID_CASES)
async def test_merge_by_id_uses_gin(session: AsyncSession, label: str, index: str) -> None:
    await bootstrap_session(session)
    plan = await _explain(session, f"MATCH (n:{label} {{id: 'x'}}) RETURN n", "n agtype")

    names = _index_names(plan)
    assert index in names, (
        f"id lookup on {label} did not use {index} (used {names}):\n{json.dumps(plan, indent=2)}"
    )
    assert not _seq_scans_on(plan, label), f"id lookup on {label} fell back to a seq scan"


@pytest.mark.parametrize(("label", "index"), VERTEX_BOX_CASES)
async def test_box_scoped_match_uses_gin(session: AsyncSession, label: str, index: str) -> None:
    await bootstrap_session(session)
    plan = await _explain(session, f"MATCH (n:{label} {{box: 'b'}}) RETURN n", "n agtype")

    names = _index_names(plan)
    assert index in names, (
        f"box scan on {label} did not use {index} (used {names}):\n{json.dumps(plan, indent=2)}"
    )
    assert not _seq_scans_on(plan, label), f"box scan on {label} fell back to a seq scan"


# (edge label, src/dst vlabel) — endpoint traversal: the Phase 2 SAME_AS / partOf walk.
EDGE_CASES = [
    ("partOf", "Object"),
    ("SAME_AS", "Actor"),
]


@pytest.mark.parametrize(("edge", "vlabel"), EDGE_CASES)
async def test_edge_traversal_uses_endpoint_index(
    session: AsyncSession, edge: str, vlabel: str
) -> None:
    await bootstrap_session(session)
    plan = await _explain(
        session,
        f"MATCH (a:{vlabel} {{id: 'x'}})-[r:{edge}]->(b:{vlabel}) RETURN b",
        "b agtype",
    )

    # The edge table must be reached by a start_id/end_id index scan, not a heap scan.
    names = _index_names(plan)
    assert f"ix_{edge.lower()}_start" in names or f"ix_{edge.lower()}_end" in names, (
        f"{edge} traversal did not use a start_id/end_id index "
        f"(used {names}):\n{json.dumps(plan, indent=2)}"
    )
    assert not _seq_scans_on(plan, edge), f"{edge} traversal fell back to a seq scan"
