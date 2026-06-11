"""Integration test fixtures.

Assumes an external Postgres+AGE+pgvector reachable via DATABASE_URL. In CI the
`tests` workflow builds the AGE image and migrates it; locally, stand up an
ephemeral DB and run `alembic upgrade head` first, then export DATABASE_URL.
"""

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Decide collection HERE, before the sibling test modules import — they import
# iknos.config, which instantiates Settings() at import and requires DATABASE_URL,
# so a missing URL would crash collection rather than skip. pytest imports this
# package conftest first, so we gate cleanly:
#   - CI + no DATABASE_URL  -> hard error: the live-DB tests must run, never silently
#     pass. (This is the false-confidence the `tests` workflow exists to prevent.)
#   - local + no DATABASE_URL -> don't collect these modules; unit tests still run.
if not os.environ.get("DATABASE_URL"):
    if os.environ.get("CI"):
        raise pytest.UsageError(
            "DATABASE_URL not set in CI — the live-DB integration tests must run, not skip"
        )
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ["DATABASE_URL"]


@pytest_asyncio.fixture
async def session(database_url: str) -> AsyncSession:
    engine = create_async_engine(database_url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with SessionLocal() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _isolate_db(session: AsyncSession) -> None:
    """Reset graph + relational data before every integration test (no shared-DB carry-over).

    The suite runs against one live DB that the `session` fixture never resets between tests. That
    was harmless while extraction idempotency keyed per-span — a content_hash collision across two
    tests just meant each extracted independently. G1.7b cross-doc reuse changes that: a committed
    extraction in *any* prior test is now replayable in a later one whose span shares its
    content_hash, coupling tests by execution order (e.g. an initial-sentence span shared between
    two documents). Cleaning before each test removes the coupling and makes order irrelevant.

    Safe because migrations seed **no data** — only extensions, the graph, empty vlabel/elabel
    definitions, and indexes — so there are no fixtures to preserve. `DETACH DELETE` clears
    vertices/edges while keeping the label *definitions*; the relational tables come from the ORM
    metadata (so a new table is auto-included) and TRUNCATE ... CASCADE handles their FK order.
    Imported lazily so the local no-DATABASE_URL collection path stays import-light.
    """
    from sqlalchemy import text

    from iknos.db.age import bootstrap_session, execute_cypher
    from iknos.db.orm import Base

    await bootstrap_session(session)
    await execute_cypher(session, "MATCH (n) DETACH DELETE n")
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    await session.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    await session.commit()
