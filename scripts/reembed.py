"""CLI for the G1.16 embedding-model reindex path.

Re-embeds every dense row to a target embedding model so a model swap is a clean migration,
not a silently mixed ANN space. The logic lives in :mod:`iknos.core.reembed` (importable +
type-checked + tested); this is the thin entry point that wires the real substrate, engine,
and AGE bootstrap.

Usage::

    uv run python -m scripts.reembed --model BAAI/bge-m3 [--batch-size 128]
"""

import argparse
import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from iknos.config import settings
from iknos.core.embeddings import EmbeddingSubstrate
from iknos.core.reembed import reembed_to_model
from iknos.db.age import bootstrap_session

logger = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-embed all dense rows to a target embedding model (G1.16)."
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Target embedding model id (e.g. BAAI/bge-m3) — every dense row is re-embedded to it.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=128, help="Propositions re-embedded per commit."
    )
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level)

    substrate = EmbeddingSubstrate(args.model)
    engine = create_async_engine(settings.database_url)
    session_local = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_local() as session:
            await bootstrap_session(session)
            report = await reembed_to_model(session, substrate, batch_size=args.batch_size)
        logger.info("done: %s", report)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
