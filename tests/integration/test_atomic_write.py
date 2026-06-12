"""W7 — dual-write transaction discipline (`db/age.py::atomic_write`) against a live Postgres+AGE.

The §10.2 hazard: an operator writes a graph artifact (raw Cypher) and appends an `Action` row
(`record_action`, which only flushes) in one logical unit. Without a rollback discipline, a failure
*after* the `Action` is buffered leaves the audit log pointing at an artifact that does not exist
(orphaned `Action`), or a half-written edge set behind a committed `Action`. `atomic_write` brackets
the unit: it commits on clean exit and rolls the whole thing back on any exception. These tests pin
both directions against the real driver — the unit-level `test_age_cypher.py` only asserts the
commit/rollback *calls* on a mock; here we assert the *durable* state.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.age import atomic_write, bootstrap_session, cypher_map, execute_cypher, merge_vertex
from iknos.provenance.action_log import record_action

pytestmark = pytest.mark.asyncio


class _InjectedFailure(RuntimeError):
    """A failure injected *between* the writes of a dual-write unit."""


async def _fact_exists(session: AsyncSession, fact_id: uuid.UUID) -> bool:
    rows = await execute_cypher(
        session,
        f"MATCH (n:Fact {cypher_map({'id': str(fact_id)})}) RETURN n.id",
        returns="id agtype",
    )
    return len(rows) > 0


async def _action_count(session: AsyncSession, actor: str) -> int:
    res = await session.execute(text("SELECT count(*) FROM actions WHERE actor = :a"), {"a": actor})
    return res.scalar_one()


async def test_atomic_write_rolls_back_graph_and_action_on_mid_unit_failure(
    session: AsyncSession,
) -> None:
    """A failure injected *after* the graph vertex is written and the `Action` is flushed rolls the
    whole unit back: no orphaned vertex, no orphaned `Action`."""
    await bootstrap_session(session)
    fact_id = uuid.uuid4()

    with pytest.raises(_InjectedFailure):
        async with atomic_write(session):
            await merge_vertex(session, "Fact", {"id": str(fact_id), "box": str(uuid.uuid4())})
            await record_action(
                session,
                actor="w7-rollback-test",
                action_type="test",
                outputs={"fact": str(fact_id)},
            )
            raise _InjectedFailure("a later write in the same unit failed")

    assert not await _fact_exists(session, fact_id)  # no orphaned vertex
    assert await _action_count(session, "w7-rollback-test") == 0  # no orphaned Action


async def test_atomic_write_commits_the_whole_unit_on_clean_exit(session: AsyncSession) -> None:
    """The success direction: the vertex and the `Action` both persist, committed together."""
    await bootstrap_session(session)
    fact_id = uuid.uuid4()

    async with atomic_write(session):
        await merge_vertex(session, "Fact", {"id": str(fact_id), "box": str(uuid.uuid4())})
        await record_action(
            session,
            actor="w7-commit-test",
            action_type="test",
            outputs={"fact": str(fact_id)},
        )

    assert await _fact_exists(session, fact_id)
    assert await _action_count(session, "w7-commit-test") == 1
