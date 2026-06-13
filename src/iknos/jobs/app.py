"""R11 — Postgres-native background job queue (procrastinate) for document ingest + extraction.

Realizes the §6 concurrency contract without new infrastructure (principle 7): procrastinate runs
on the existing Postgres via ``LISTEN``/``NOTIFY``. A box's jobs serialize through one queue and
no two jobs for the *same* document run concurrently — so an investigation's graph writes don't
race as in-process foreground jobs (the failure mode a real V1 gate-corpus ingest would hit).

**Queue-scope decision (2026-06-12 review — the gate dry-run report flagged "the job covers
ingest only").** Perception (parse → segment → embed → persist ``Span``s) and extraction
(propositionization — the LLM pass that yields ``Proposition``s + faithfulness) are **two tasks**,
not one job:

- they have different cost/retry profiles — embedding is CPU/GPU-local, extraction is LLM-bound;
  folding them into one job would **re-embed on every LLM transport blip**, and one retry policy
  cannot fit both failure surfaces;
- so :func:`ingest_document_bytes_job` does perception and, **on success**, enqueues
  :func:`propositionize_document_job` as a follow-on. The follow-on shares the box's queue +
  execution ``lock``, so it serializes after the box's perception work (no graph-write race), and
  carries its own ``queueing_lock`` so it is never double-queued. Both are content-hash idempotent,
  so a re-fired chain is a no-op. (Whether *re*-inference fires is the separate VoI/budget policy of
  §6.1/§11.1 — Phase 5; this only chains the *initial* extraction of freshly-ingested spans.)
- the follow-on runs in its own process from a document id, so it reloads spans via
  ``core.ingest.load_document_spans`` (the read-path inverse of ``persist_spans``).

Structure:

- :data:`app` — the procrastinate App bound to ``DATABASE_URL`` (lazy connector; importing this
  module needs no live DB, so the unit tests and the API import it freely).
- :data:`RETRYABLE_INGEST_EXCEPTIONS` / :func:`is_retryable_ingest_error` — the retry policy shared
  by both tasks: transport-class failures (HTTP transport/timeout, transient DB connection loss) are
  transient and retried with exponential backoff up to :data:`MAX_ATTEMPTS`; **validation / bad-data
  / programming errors are terminal** (``DocumentResegmentationError``, ``StaleExtractionError``, a
  pydantic ``ValidationError`` …), because waiting cannot fix them.
- :func:`ingest_document_bytes_job` / :func:`propositionize_document_job` — the tasks; each
  delegates to a ``_*_one`` module function kept separate so a test can patch it and exercise the
  queue / lock / retry / chaining wiring without loading torch, an LLM, or a graph DB.

Both worker tasks build their own engine per job and register the AGE connect-bootstrap on it
(``db.session.register_age_bootstrap``) — a worker engine is **not** the app engine, so without it
``cypher()`` would hit a connection where the ``age`` extension is unloaded.

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
    no graph DB) while the production path builds the heavy backend/segmenter/parser and calls
    ``core.ingest.ingest_document_bytes``. The embedding backend is constructed through the R10
    :func:`~iknos.core.embeddings.make_embedding_backend` seam, so an unset ``EMBEDDINGS_BASE_URL``
    keeps the in-process bge-m3 substrate (torch in the worker, byte-identical to before) while a
    set one routes embedding to the hosted service — the worker then need not hold torch. ``core.
    ingest`` takes the ``EmbeddingBackend`` protocol, so either backend is a drop-in.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iknos.core.embeddings import make_embedding_backend
    from iknos.core.ingest import ingest_document_bytes
    from iknos.core.mineru import make_parser
    from iknos.core.segmentation import SegmentationBackbone, default_level_policy
    from iknos.db.age import atomic_write
    from iknos.db.session import register_age_bootstrap

    engine = register_age_bootstrap(create_async_engine(settings.database_url))
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    # R10 seam: EMBEDDINGS_BASE_URL empty → in-process substrate; set → HTTP backend (no torch).
    substrate = make_embedding_backend()
    parser = make_parser()
    segmenter = SegmentationBackbone(levels=default_level_policy())
    try:
        # core.ingest is caller-owned-transaction (it does not commit); the worker is that
        # caller, so the spans + their Actions commit together via atomic_write — without this
        # the session closes and rolls back, the job still reports success, and nothing lands.
        async with session_factory() as session, atomic_write(session):
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
    to :data:`MAX_ATTEMPTS` (see :data:`RETRYABLE_INGEST_EXCEPTIONS`). On success it chains the
    follow-on extraction task (see the module docstring's queue-scope decision); a failed ingest
    raises before the chain, so nothing is extracted from a document that did not land.
    """
    await _ingest_one(
        document_id=uuid.UUID(document_id),
        document_bytes=base64.b64decode(content_b64),
        media_type=media_type,
        title=title,
    )
    # Follow-on extraction (chained only on a successful ingest). Same box queue + execution lock so
    # it serializes after perception; its own queueing_lock so the chain cannot double-queue it.
    await propositionize_document_job.configure(
        queue=_box_queue(box),
        lock=box,
        queueing_lock=f"propositionize:{document_id}",
    ).defer_async(document_id=document_id, box=box)


def _box_queue(box: str | None) -> str:
    """The box's serialized work queue (``ingest:<box>``) or the default queue (no box)."""
    return f"ingest:{box}" if box else "ingest"


async def _propositionize_one(*, document_id: uuid.UUID) -> None:
    """Extract one already-ingested document's propositions (one session). Patched out in tests.

    The read-path inverse of ingest: reloads the document's level-0 spans + raw text, builds the
    Propositionizer from config — the LLM extractor, the optional independent verifier (G1.4; absent
    when ``LLM_VERIFIER_MODEL`` is unset → faithfulness stays null, the documented degraded mode),
    and the R10 embedding backend — and runs ``propositionize_document``. Idempotent on each span's
    pipeline hash, so a re-fired chain re-extracts nothing.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iknos.core.embeddings import make_embedding_backend
    from iknos.core.ingest import load_document_spans, load_document_text
    from iknos.core.llm import LLMClient
    from iknos.core.proposition import Propositionizer
    from iknos.core.verify import Verifier
    from iknos.db.age import atomic_write
    from iknos.db.session import register_age_bootstrap

    engine = register_age_bootstrap(create_async_engine(settings.database_url))
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    substrate = make_embedding_backend()
    # n_samples > 1 needs a temperature > 0 (the require_sampling_diversity guard); 1 stays greedy.
    sampling: dict[str, object] = {"temperature": 0.0 if settings.llm_extract_samples <= 1 else 0.7}
    verifier = (
        Verifier(
            LLMClient(base_url=settings.llm_verifier_base_url, model=settings.llm_verifier_model)
        )
        if settings.llm_verifier_model
        else None
    )
    propositionizer = Propositionizer(
        LLMClient(),  # LLM_BASE_URL / LLM_MODEL from config
        substrate,
        sampling=sampling,
        verifier=verifier,
        n_samples=settings.llm_extract_samples,
        agreement_threshold=settings.prop_agreement_threshold,
        reuse_extractions=settings.extract_reuse_enabled,
    )
    try:
        # Caller-owned transaction, like ingest: the extracted propositions + their Actions commit
        # together via atomic_write (an early no-op return commits nothing — harmless).
        async with session_factory() as session, atomic_write(session):
            spans = await load_document_spans(session, document_id)
            if not spans:
                return  # document never ingested at the extraction level — a clean no-op
            raw_text = await load_document_text(session, document_id)
            if raw_text is None:
                return
            await propositionizer.propositionize_document(session, document_id, spans, raw_text)
    finally:
        substrate.close()
        await engine.dispose()


@app.task(
    name="propositionize_document_job",
    retry=RetryStrategy(
        max_attempts=MAX_ATTEMPTS,
        exponential_wait=2,
        retry_exceptions=list(RETRYABLE_INGEST_EXCEPTIONS),
    ),
    queue="ingest",
)
async def propositionize_document_job(*, document_id: str, box: str | None = None) -> None:
    """Extract one already-ingested document's propositions — the follow-on to ingest.

    Enqueued by :func:`ingest_document_bytes_job` on a successful ingest (the module docstring's
    queue-scope decision), or directly by an operator. Same retry classification as ingest: an LLM
    transport blip retries; a stale-pipeline / validation / no-model error is terminal. ``box`` is
    queue/lock metadata only.
    """
    await _propositionize_one(document_id=uuid.UUID(document_id))
