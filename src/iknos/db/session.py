"""Async session factory for the API + the reusable AGE connect-bootstrap.

Every new physical connection runs `LOAD 'age'` and sets search_path so cypher() calls work
without per-query bootstrap. This is registered as a **connection-level** event (run once at
connect, in autocommit) rather than a per-session ``SET``: a per-session ``SET search_path`` is
transactional and is **reset by ROLLBACK**, which ingest and propositionization perform per item
for isolation (G1.17 R1) — so a per-session bootstrap would silently stop working after the first
isolated failure. ``db/age.bootstrap_session`` is the per-session variant, safe only for
rollback-free sessions (scripts/tests that commit once).
"""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from iknos.config import settings


def register_age_bootstrap(engine: AsyncEngine) -> AsyncEngine:
    """Attach the per-connection AGE bootstrap (``LOAD 'age'`` + search_path) to ``engine``.

    Returns the engine for chaining. Use for **any** engine built outside this module — the R11
    ingest worker constructs its own engine per job and would otherwise issue ``cypher()`` against
    a connection where the ``age`` extension is not loaded and ``ag_catalog`` is off the
    search_path (``function ag_catalog.cypher(...) does not exist``). Connection-level, so it
    survives the per-item rollbacks the ingest/extract paths use (see the module docstring).
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _bootstrap_age(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("LOAD 'age'")
        cursor.execute('SET search_path = ag_catalog, "$user", public')
        cursor.close()

    return engine


_engine = register_age_bootstrap(create_async_engine(settings.database_url, pool_pre_ping=True))
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with _SessionLocal() as session:
        yield session
