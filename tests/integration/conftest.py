"""Integration test fixtures.

Assumes an external Postgres+AGE+pgvector reachable via DATABASE_URL.
Bring one up with `docker compose up -d postgres migrate` before running.
"""

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; bring up postgres+migrate first")
    return url


@pytest_asyncio.fixture
async def session(database_url: str) -> AsyncSession:
    engine = create_async_engine(database_url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with SessionLocal() as s:
        yield s
    await engine.dispose()
