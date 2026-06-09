"""Integration test: a domain pack loads end-to-end into AGE (G0.7 exit criterion).

Proves the declare → validate → persist path against a live graph: the Box
registry vertex, the taxonomy Objects, the declared ``directPartOf`` edges, and
the derived ``partOf`` closure are all written and box-scoped; the §14 roll-up
rule holds in the graph (roller → pump rolls up, steel → pump does not); and a
re-load is idempotent (no duplicates).
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.age import bootstrap_session, execute_cypher
from iknos.domain.loader import PackImmutabilityError, list_active_packs, load_pack
from iknos.domain.packs import bundled_pack

pytestmark = pytest.mark.asyncio


async def _count(session: AsyncSession, query: str) -> int:
    rows = await execute_cypher(session, query, returns="n agtype")
    return int(rows[0][0]) if rows else 0


async def _scalar(session: AsyncSession, query: str) -> str | None:
    rows = await execute_cypher(session, query, returns="v agtype")
    if not rows or rows[0][0] is None:
        return None
    return str(rows[0][0]).strip('"')


async def test_pack_loads_end_to_end(session: AsyncSession) -> None:
    await bootstrap_session(session)
    pack = bundled_pack("pump_basic")
    box_id = pack.box_id

    result = await load_pack(session, pack)
    await session.commit()

    assert result.box_id == box_id
    assert result.direct_part_of == 4
    assert result.part_of == 5  # 4 direct + 1 component-integral roll-up

    # --- Box registry vertex, marked as a domain pack and active ---
    assert (
        await _count(
            session,
            f"MATCH (b:Box {{id: '{box_id}', kind: 'domain_pack', status: 'active'}}) "
            "RETURN count(b)",
        )
        == 1
    )

    # --- taxonomy Objects, all box-tagged ---
    assert (
        await _count(
            session,
            f"MATCH (o:Object {{box: '{box_id}'}}) RETURN count(o)",
        )
        == 5
    )

    # --- declared directPartOf edges and the partOf closure, box-scoped ---
    assert (
        await _count(
            session,
            f"MATCH (:Object)-[r:directPartOf {{box: '{box_id}'}}]->(:Object) RETURN count(r)",
        )
        == 4
    )
    assert (
        await _count(
            session,
            f"MATCH (:Object)-[r:partOf {{box: '{box_id}'}}]->(:Object) RETURN count(r)",
        )
        == 5
    )

    # --- §14: roller rolls up to pump through two component-integral hops ---
    roller, pump, steel = (
        pack.entity_id("roller"),
        pack.entity_id("pump"),
        pack.entity_id("steel"),
    )
    rows = await execute_cypher(
        session,
        f"MATCH (:Object {{id: '{roller}'}})-[r:partOf]->(:Object {{id: '{pump}'}}) "
        "RETURN r.derivation",
        returns="d agtype",
    )
    assert len(rows) == 1
    assert str(rows[0][0]).strip('"') == "rollup"

    # --- §14: steel (stuff-object) must NOT roll up to pump ---
    assert (
        await _count(
            session,
            f"MATCH (:Object {{id: '{steel}'}})-[r:partOf]->(:Object {{id: '{pump}'}}) "
            "RETURN count(r)",
        )
        == 0
    )

    # --- activation lookup surfaces the loaded pack ---
    active = await list_active_packs(session)
    assert any(p["id"] == str(box_id) and p["name"] == "pump-basic" for p in active)


async def test_reload_is_idempotent(session: AsyncSession) -> None:
    await bootstrap_session(session)
    pack = bundled_pack("pump_basic")
    box_id = pack.box_id

    first = await load_pack(session, pack)
    await session.commit()
    valid_from_after_first = await _scalar(
        session, f"MATCH (b:Box {{id: '{box_id}'}}) RETURN b.valid_from"
    )
    edge_valid_from_after_first = await _scalar(
        session,
        f"MATCH (:Object)-[r:partOf {{box: '{box_id}'}}]->(:Object) RETURN r.valid_from LIMIT 1",
    )

    second = await load_pack(session, pack)
    await session.commit()

    assert second.already_loaded is True
    assert first.box_id == second.box_id

    # G0.R1: a re-load is a true no-op — the bitemporal valid_from is preserved,
    # not silently rewritten to "now". (This is the assertion whose absence hid
    # the original bug; counts alone could not catch it.)
    assert valid_from_after_first is not None
    assert (
        await _scalar(session, f"MATCH (b:Box {{id: '{box_id}'}}) RETURN b.valid_from")
        == valid_from_after_first
    )
    assert (
        await _scalar(
            session,
            f"MATCH (:Object)-[r:partOf {{box: '{box_id}'}}]->(:Object) "
            "RETURN r.valid_from LIMIT 1",
        )
        == edge_valid_from_after_first
    )

    # No duplication: still exactly one Box, five Objects, five partOf edges.
    assert await _count(session, f"MATCH (b:Box {{id: '{box_id}'}}) RETURN count(b)") == 1
    assert await _count(session, f"MATCH (o:Object {{box: '{box_id}'}}) RETURN count(o)") == 5
    assert (
        await _count(
            session,
            f"MATCH (:Object)-[r:partOf {{box: '{box_id}'}}]->(:Object) RETURN count(r)",
        )
        == 5
    )


async def test_first_load_stamps_content_hash(session: AsyncSession) -> None:
    await bootstrap_session(session)
    pack = bundled_pack("pump_basic")

    await load_pack(session, pack)
    await session.commit()

    stored = await _scalar(session, f"MATCH (b:Box {{id: '{pack.box_id}'}}) RETURN b.content_hash")
    assert stored == pack.content_hash


async def test_changed_content_same_version_is_rejected(session: AsyncSession) -> None:
    await bootstrap_session(session)
    pack = bundled_pack("pump_basic")

    await load_pack(session, pack)
    await session.commit()

    # Same (name, version) — therefore the same Box id — but different content.
    # A pack version is immutable: this must fail loudly, not silently diverge.
    mutated = pack.model_copy(update={"reliability_prior": 0.5})
    assert mutated.box_id == pack.box_id
    assert mutated.content_hash != pack.content_hash

    with pytest.raises(PackImmutabilityError, match="immutable"):
        await load_pack(session, mutated)
