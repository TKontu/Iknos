"""G3.7 integration test — `SAME_AS`-component aggregation + merge/split belief revision.

Real Postgres+AGE. Two base ``Fact``s mention two *duplicate* ``Actor`` mentions of one real
entity. Before a merge they aggregate to two separate canonicals; a :meth:`ComponentReasoner.merge`
(assert `SAME_AS`) re-aggregates support **additively** (1+1=2) and confidence by **max** onto
one canonical; a :meth:`ComponentReasoner.split` (retract it, bitemporally) separates them
again — "over-merging is recoverable" (§5.2). Both revisions leave a joinable Action (§10.1).

``aggregate`` reads the whole active subgraph, so assertions target *my* entities/canonical.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.component_aggregate import ComponentReasoner
from iknos.db.age import bootstrap_session, merge_edge, merge_vertex
from iknos.types.nodes import Box

pytestmark = pytest.mark.asyncio


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_fact_with_actor(
    session: AsyncSession, box: uuid.UUID, *, label: str, confidence: float
) -> tuple[uuid.UUID, uuid.UUID]:
    """A base Fact (EVIDENCED_BY a Span) involving a fresh Actor mention. Returns (fact, actor)."""
    fid, sid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await merge_vertex(
        session,
        "Fact",
        {"id": str(fid), "box": str(box), "confidence": confidence, "valid_to": None},
    )
    await merge_vertex(
        session, "Span", {"id": str(sid), "document_id": str(uuid.uuid4()), "start": 0, "end": 5}
    )
    await merge_edge(session, src_id=fid, dst_id=sid, label="EVIDENCED_BY", props={"box": str(box)})
    await merge_vertex(
        session, "Actor", {"id": str(eid), "box": str(box), "label": label, "type": "person"}
    )
    await merge_edge(
        session,
        src_id=fid,
        dst_id=eid,
        label="INVOLVES",
        props={"role": "subject", "box": str(box)},
    )
    return fid, eid


async def _action_count(session: AsyncSession, action_type: str) -> int:
    row = await session.execute(
        text("SELECT count(*) FROM actions WHERE actor='belief-revision' AND action_type=:t"),
        {"t": action_type},
    )
    return int(row.scalar_one())


async def test_merge_then_split_aggregates_and_recovers(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("g37-agg", "1", "test", 0.8)
    await _put_box(session, box)

    _, e1 = await _put_fact_with_actor(session, box.id, label="operator", confidence=0.7)
    _, e2 = await _put_fact_with_actor(session, box.id, label="the operator", confidence=0.9)
    await session.commit()
    e1s, e2s = str(e1), str(e2)
    canonical = min(e1s, e2s)

    reasoner = ComponentReasoner()

    # Before any merge: two separate canonical entities, each supported by one fact.
    before = await reasoner.aggregate(session)
    assert e1s in before and e2s in before
    assert before[e1s].support_count == 1
    assert before[e2s].support_count == 1

    # Merge: evidence accrues to one canonical — support 1+1=2, confidence max(0.7,0.9)=0.9.
    rev = await reasoner.merge(session, e1, e2, box=box.id, strength=0.95)
    merged = rev.components
    assert canonical in merged
    other = e2s if canonical == e1s else e1s
    assert other not in merged  # absorbed into the canonical
    assert merged[canonical].support_count == 2
    assert merged[canonical].confidence == pytest.approx(0.9)
    assert merged[canonical].members == frozenset({e1s, e2s})
    assert await _action_count(session, "revise_components") >= 1

    # Split: retract the SAME_AS (bitemporal) — the entities separate again, evidence recovers.
    rev2 = await reasoner.split(session, e1, e2)
    split = rev2.components
    assert e1s in split and e2s in split
    assert split[e1s].support_count == 1
    assert split[e2s].support_count == 1
    assert rev2.action_id != rev.action_id


async def test_aggregate_excludes_candidate_same_as(session: AsyncSession) -> None:
    # A candidate (non-confirmed) SAME_AS must NOT merge the components (§5.2 conservative).
    await bootstrap_session(session)
    box = case_box("g37-candidate", "1", "test", 0.8)
    await _put_box(session, box)
    _, e1 = await _put_fact_with_actor(session, box.id, label="bearing", confidence=0.5)
    _, e2 = await _put_fact_with_actor(session, box.id, label="bearing", confidence=0.5)

    # Write a *candidate* SAME_AS directly (below the confirm bar).
    from datetime import UTC, datetime

    from iknos.core.resolve import same_as_to_props
    from iknos.types.edges import SameAsState

    src, dst = sorted((e1, e2), key=str)
    await merge_edge(
        session,
        src_id=src,
        dst_id=dst,
        label="SAME_AS",
        props=same_as_to_props(
            box=box.id, state=SameAsState.CANDIDATE, strength=0.6, now=datetime.now(UTC)
        ),
    )
    await session.commit()

    agg = await ComponentReasoner().aggregate(session)
    # Candidate keeps them separate: two canonicals, not one merged component.
    assert str(e1) in agg and str(e2) in agg
    assert agg[str(e1)].support_count == 1
    assert agg[str(e2)].support_count == 1
