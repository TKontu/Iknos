"""Smoke test for migration 0004 (docs/gap_phase_0_foundations.md G0.2-G0.3).

Proves the new vertex/edge labels added by 0004_schema_revision are not just
migrate-able (the up/down/up CI gate covers that) but actually *usable*: a node
of each new vlabel stores, and an edge of each new elabel can be created between
nodes and matched back. Endpoint labels follow the intended §10 semantics, but
AGE does not enforce them — the point here is label create-ability, not the
property/endpoint contract (which the Pydantic projections enforce in later
phases).
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.age import bootstrap_session, cypher_map, execute_cypher

pytestmark = pytest.mark.asyncio


# (source vlabel, edge label, target vlabel) — intended §10 endpoints.
NEW_EDGES = (
    ("Mention", "REFERS_TO", "Actor"),
    ("Actor", "SAME_AS", "Actor"),
    ("Object", "directPartOf", "Object"),
    ("Object", "partOf", "Object"),
    ("Task", "DECOMPOSES_INTO", "Task"),
    ("Task", "ADDRESSES", "Fact"),
    ("Fact", "RELEVANT_TO", "Task"),
)


async def test_new_vertex_labels_usable(session: AsyncSession) -> None:
    await bootstrap_session(session)

    for label in ("Mention", "Task"):
        node_id = uuid.uuid4()
        await execute_cypher(
            session,
            f"CREATE (:{label} {cypher_map({'id': str(node_id)})})",
        )
        rows = await execute_cypher(
            session,
            f"MATCH (n:{label} {{id: '{node_id}'}}) RETURN n",
            returns="n agtype",
        )
        assert len(rows) == 1, f"vlabel {label} not usable"


async def test_new_edge_labels_usable(session: AsyncSession) -> None:
    await bootstrap_session(session)

    for src_label, edge_label, dst_label in NEW_EDGES:
        src_id, dst_id = uuid.uuid4(), uuid.uuid4()
        await execute_cypher(
            session,
            f"CREATE (:{src_label} {cypher_map({'id': str(src_id)})})",
        )
        await execute_cypher(
            session,
            f"CREATE (:{dst_label} {cypher_map({'id': str(dst_id)})})",
        )
        await execute_cypher(
            session,
            f"MATCH (a:{src_label} {{id: '{src_id}'}}), (b:{dst_label} {{id: '{dst_id}'}}) "
            f"CREATE (a)-[:{edge_label}]->(b)",
        )
        rows = await execute_cypher(
            session,
            f"MATCH (:{src_label} {{id: '{src_id}'}})-[r:{edge_label}]->(:{dst_label}) RETURN r",
            returns="r agtype",
        )
        assert len(rows) == 1, f"elabel {edge_label} not usable"
