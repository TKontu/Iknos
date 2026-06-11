"""Integration test: the box registry against a live AGE graph (G2.1; §9, §10.1).

Proves the create → read → scope → deprecate path: a case box round-trips through
``get_box``; a re-create is a true no-op with the bitemporal ``valid_from`` preserved
(the G0.R1 discipline, generalized to all boxes); tier/status scoping orders by
reliability; deprecation closes the box; every create emits exactly one ``create-box``
Action; and the consolidated pack loader now emits the same Action.

Re-run safe: boxes use deterministic ids (``case_box`` derives id from name+version) and
``create_box`` no-ops on an existing box, so a create-box Action count stays 1 across
repeated suite runs. Tests use distinct names to avoid cross-test interference. The pack
test is the exception — ``load_pack`` emits its create-box Action only on a *first* load,
so it purges any prior pump-basic Box first to force one (don't depend on a pristine DB).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.registry import (
    active_boxes_by_tier,
    create_box,
    deprecate_box,
    get_box,
    list_boxes,
)
from iknos.boxes.serde import case_box
from iknos.db.age import bootstrap_session, execute_cypher
from iknos.domain.loader import load_pack
from iknos.domain.packs import bundled_pack
from iknos.types.governance import SourceInterest
from iknos.types.nodes import BoxStatus, Tier

pytestmark = pytest.mark.asyncio


async def _scalar(session: AsyncSession, query: str) -> str | None:
    rows = await execute_cypher(session, query, returns="v agtype")
    if not rows or rows[0][0] is None:
        return None
    return str(rows[0][0]).strip('"')


async def _create_box_action_count(session: AsyncSession, box_id: str) -> int:
    row = await session.execute(
        text(
            "SELECT count(*) FROM actions "
            "WHERE action_type = 'create-box' AND outputs->>'box' = :id"
        ),
        {"id": box_id},
    )
    return int(row.scalar_one())


async def test_case_box_round_trips_through_get_box(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box(
        "br-roundtrip",
        "1",
        "case.pdf",
        0.8,
        interest=SourceInterest(role="supplier", stake={"x"}),
        valid_from=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )

    returned = await create_box(session, box)
    await session.commit()
    assert returned == box

    got = await get_box(session, box.id)
    assert got == box  # full round-trip incl. tier, interest, valid_from, status


async def test_recreate_is_noop_and_preserves_valid_from(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("br-idem", "1", "case.pdf", 0.7)

    await create_box(session, box)
    await session.commit()
    vf = await _scalar(session, f"MATCH (b:Box {{id: '{box.id}'}}) RETURN b.valid_from")
    assert vf is not None

    # Same (name, version) -> same id, but different content. A re-create must NOT write:
    # it returns the stored box and never moves valid_from (the G0.R1 discipline).
    returned = await create_box(session, case_box("br-idem", "1", "other.pdf", 0.2))
    await session.commit()

    assert returned.reliability_prior == 0.7  # the stored box, not the 0.2 re-create
    assert await _scalar(session, f"MATCH (b:Box {{id: '{box.id}'}}) RETURN b.valid_from") == vf
    # Exactly one create-box Action — the no-op emitted none.
    assert await _create_box_action_count(session, str(box.id)) == 1


async def test_scope_by_tier_orders_by_reliability(session: AsyncSession) -> None:
    await bootstrap_session(session)
    hi = case_box("br-ord-hi", "1", "s.pdf", 0.95)
    lo = case_box("br-ord-lo", "1", "s.pdf", 0.15)
    await create_box(session, hi)
    await create_box(session, lo)
    await session.commit()

    by_tier = await active_boxes_by_tier(session, [Tier.CASE])
    ids = [b.id for b in by_tier]
    assert hi.id in ids and lo.id in ids
    assert ids.index(hi.id) < ids.index(lo.id)  # higher reliability first

    listed = {b.id for b in await list_boxes(session, tier=Tier.CASE)}
    assert {hi.id, lo.id} <= listed


async def test_deprecate_closes_box_and_preserves_valid_from(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("br-dep", "1", "case.pdf", 0.5)
    await create_box(session, box)
    await session.commit()
    vf = await _scalar(session, f"MATCH (b:Box {{id: '{box.id}'}}) RETURN b.valid_from")

    await deprecate_box(session, box.id)
    await session.commit()

    got = await get_box(session, box.id)
    assert got is not None
    assert got.status == BoxStatus.DEPRECATED
    assert got.valid_to is not None
    assert got.valid_from.isoformat() == vf  # valid_from untouched by deprecation

    # No longer surfaced by the active-only default scope.
    assert box.id not in {b.id for b in await list_boxes(session, tier=Tier.CASE)}


async def test_pack_load_emits_create_box_action(session: AsyncSession) -> None:
    # The consolidated loader writes its Box through the shared layer and emits the same
    # create-box Action (uniform auditability across pack and general boxes).
    await bootstrap_session(session)
    pack = bundled_pack("pump_basic")
    # Unlike create_box (whose Action count stays 1 across re-runs because the original
    # persists), load_pack emits the create-box Action *only on a real first load* and
    # no-ops on an existing Box. So this assertion needs a genuine first load — purge any
    # pump-basic Box + its create-box Action left by a prior suite run on the shared DB.
    await execute_cypher(session, f"MATCH (b:Box {{id: '{pack.box_id}'}}) DETACH DELETE b")
    await session.execute(
        text("DELETE FROM actions WHERE action_type = 'create-box' AND outputs->>'box' = :id"),
        {"id": str(pack.box_id)},
    )
    await session.commit()

    await load_pack(session, pack)
    await session.commit()

    row = await session.execute(
        text(
            "SELECT actor FROM actions WHERE action_type = 'create-box' AND outputs->>'box' = :id"
        ),
        {"id": str(pack.box_id)},
    )
    actors = {r[0] for r in row.all()}
    assert "pack-loader" in actors
