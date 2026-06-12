"""R11 — Postgres-native background job queue (procrastinate) for document ingest.

Realizes the §6 concurrency contract without new infrastructure (principle 7): procrastinate runs
on the existing Postgres via ``LISTEN``/``NOTIFY``. A box's ingests serialize through one queue and
no two jobs for the *same* document run concurrently — so an investigation's graph writes don't
race as in-process foreground jobs (the failure mode a real V1 gate-corpus ingest would hit).

Structure:

- :data:`app` — the procrastinate App bound to ``DATABASE_URL`` (lazy connector; importing this
  module needs no live DB, so the unit tests and the API import it freely).
- :data:`RETRYABLE_INGEST_EXCEPTIONS` / :func:`is_retryable_ingest_error` — the retry policy:
  transport-class failures (HTTP transport/timeout, transient DB connection loss) are transient and
  retried with exponential backoff up to :data:`MAX_ATTEMPTS`; **validation / bad-data / programming
  errors are terminal** (``DocumentResegmentationError``, ``EmbeddingModelMismatchError``, a
  parse/tiling ``ValueError``, a pydantic ``ValidationError`` …), because waiting cannot fix them.
- :func:`ingest_document_bytes_job` — the task. It decodes the bytes and delegates to
  :func:`_ingest_one`, kept separate so a test can patch it and exercise the queue/lock/retry wiring
  without loading torch or touching a graph DB.

The synchronous in-process callers of ``core.ingest`` are untouched — this is an *additional*
entry point, not a replacement.
"""

from __future__ import annotations

import base64
import uuid

import httpx
from procrastinate import App, PsycopgConnector, RetryStrategy
from sqlalchemy.exc import InterfaceError, OperationalError

from iknos.config import settings

# Retry budget for a transient ingest failure (R11): at most this many attempts in total (the
# initial run + retries), with exponential backoff between them.
MAX_ATTEMPTS = 3

# Transport-class failures only — see the module docstring. Anything not in this tuple is terminal.
# A dropped DB connection mid-write is transient and a re-run is safe (ingest is idempotent on the
# document's content hash). HTTP errors come from the parser/embedding service edges.
RETRYABLE_INGEST_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.TransportError,
    OperationalError,
    InterfaceError,
)


def is_retryable_ingest_error(exc: BaseException) -> bool:
    """Whether an ingest failure is transient (retry) or terminal (fail the job). Pure; unit-tested.

    Mirrors ``core/mineru.py``'s split: transport/timeout/connection errors are transient; a
    validation or programming error is a bug or bad data that retrying only re-burns.
    """
    return isinstance(exc, RETRYABLE_INGEST_EXCEPTIONS)


def _psycopg_conninfo(database_url: str) -> str:
    """Turn the SQLAlchemy asyncpg URL into a libpq conninfo for procrastinate's psycopg connector.

    ``DATABASE_URL`` is ``postgresql+asyncpg://…`` (the SQLAlchemy driver form); psycopg wants the
    bare ``postgresql://…``. Only the ``+asyncpg`` driver tag is stripped, so both stacks talk to
    the same database.
    """
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


app = App(
    connector=PsycopgConnector(conninfo=_psycopg_conninfo(settings.database_url)),
    import_paths=["iknos.jobs.app"],
)


async def _ingest_one(
    *,
    document_id: uuid.UUID,
    document_bytes: bytes,
    media_type: str,
    title: str | None,
) -> None:
    """Run the real document ingest (one session/transaction). Patched out in queue-wiring tests.

    Kept as a module function so the queue/lock/retry behaviour is exercised with a fake (no torch,
    no graph DB) while the production path builds the heavy substrate/segmenter/parser and calls
    ``core.ingest.ingest_document_bytes``. The embedding backend is the in-process substrate today,
    constructed per job; the R10 ``make_embedding_backend`` factory is the seam to route it
    out-of-process (so the worker need not hold torch) once ``core.ingest`` takes the backend
    protocol — until then a long-running worker reloads the model per document.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iknos.core.embeddings import EmbeddingSubstrate
    from iknos.core.ingest import ingest_document_bytes
    from iknos.core.mineru import make_parser
    from iknos.core.segmentation import SegmentationBackbone, default_level_policy

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    substrate = EmbeddingSubstrate()  # the default bge-m3; R10's make_embedding_backend is the
    # out-of-process seam (so the worker need not hold torch) once core.ingest takes the protocol.
    parser = make_parser()
    segmenter = SegmentationBackbone(levels=default_level_policy())
    try:
        async with session_factory() as session:
            await ingest_document_bytes(
                session,
                document_id,
                document_bytes,
                substrate,
                segmenter,
                media_type=media_type,
                parser=parser,
                title=title,
            )
    finally:
        substrate.close()
        aclose = getattr(parser, "aclose", None)
        if aclose is not None:
            await aclose()
        await engine.dispose()


@app.task(
    name="ingest_document_bytes_job",
    retry=RetryStrategy(
        max_attempts=MAX_ATTEMPTS,
        exponential_wait=2,
        retry_exceptions=list(RETRYABLE_INGEST_EXCEPTIONS),
    ),
    queue="ingest",
)
async def ingest_document_bytes_job(
    *,
    document_id: str,
    content_b64: str,
    media_type: str = "text/plain",
    title: str | None = None,
    box: str | None = None,
) -> None:
    """Ingest one uploaded document's bytes (the queued counterpart of ``ingest_document_bytes``).

    Enqueued by ``api/main.py`` with a per-box queue + a ``queueing_lock`` on the document id, so a
    document is never ingested by two jobs at once and a box's ingests serialize. The bytes arrive
    base64-encoded (``content_b64``); validation failures are terminal, transport failures retry up
    to :data:`MAX_ATTEMPTS` (see :data:`RETRYABLE_INGEST_EXCEPTIONS`).
    """
    await _ingest_one(
        document_id=uuid.UUID(document_id),
        document_bytes=base64.b64decode(content_b64),
        media_type=media_type,
        title=title,
    )
