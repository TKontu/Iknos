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
