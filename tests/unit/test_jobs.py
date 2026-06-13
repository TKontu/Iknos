"""R11 — the procrastinate ingest job: retry classification + enqueue/run wiring (no live DB).

The pure retry classifier is tested directly; the queue/lock/retry behaviour is exercised against
``procrastinate.testing.InMemoryConnector`` with :func:`_ingest_one` patched to a fake, so no torch
loads and no graph DB is touched. Importing ``iknos.jobs.app`` constructs the App from
``DATABASE_URL`` (the connector is lazy — no connection), so a dummy is set before import; this is a
no-op when a real ``DATABASE_URL`` is already present (CI / integration).
"""

from __future__ import annotations

import base64
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402
from procrastinate.testing import InMemoryConnector  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from iknos.core.ingest import DocumentResegmentationError  # noqa: E402
from iknos.core.proposition import CascadeDependentsError, StaleExtractionError  # noqa: E402
from iknos.jobs.app import (  # noqa: E402
    MAX_ATTEMPTS,
    IngestRetryableError,
    _raise_for_failed_spans,
    app,
    ingest_document_bytes_job,
    is_retryable_ingest_error,
    propositionize_document_job,
)

# --- the pure retry classifier ----------------------------------------------------------------


def test_transport_class_errors_are_retryable() -> None:
    req = httpx.Request("POST", "http://embed.invalid")
    assert is_retryable_ingest_error(httpx.ConnectError("down", request=req)) is True
    assert is_retryable_ingest_error(httpx.ReadTimeout("slow", request=req)) is True
    assert (
        is_retryable_ingest_error(OperationalError("SELECT 1", {}, Exception("conn lost"))) is True
    )


def test_llm_transport_errors_are_retryable() -> None:
    # R11-H item 1: a momentary vLLM blip during extraction must retry, not fail the job. The LLM
    # client (core/llm.py) raises these on transport trouble / its R5 deadline; none were retryable
    # before, so an endpoint flap marked the job terminally `failed`.
    req = httpx.Request("POST", "http://vllm.invalid")
    assert is_retryable_ingest_error(openai.APIConnectionError(request=req)) is True
    assert is_retryable_ingest_error(openai.APITimeoutError(request=req)) is True
    resp = httpx.Response(500, request=req)
    body = {"error": {"message": "boom"}}
    assert (
        is_retryable_ingest_error(openai.InternalServerError("boom", response=resp, body=body))
        is True
    )
    assert is_retryable_ingest_error(TimeoutError("hung endpoint")) is True  # asyncio.timeout R5
    # The worker's own re-fire signal (an all/partially-failed extraction report) is retryable too.
    assert is_retryable_ingest_error(IngestRetryableError("failed spans")) is True


def test_validation_and_programming_errors_are_terminal() -> None:
    # The named validation errors and any unexpected/programming error must NOT retry. An HTTP
    # status error is terminal at the job level too: the parser/embedding clients already retry 5xx
    # internally (core/mineru.py) and re-raise on exhaustion, so a job-level retry would not help.
    assert is_retryable_ingest_error(DocumentResegmentationError("resegment")) is False
    # Extraction-side validation errors stay terminal (R11-H item 1): retrying cannot fix stale
    # pipeline state or a cascade-dependents violation.
    assert is_retryable_ingest_error(StaleExtractionError("stale")) is False
    assert is_retryable_ingest_error(CascadeDependentsError("cascade")) is False
    assert is_retryable_ingest_error(ValueError("bad tiling")) is False
    assert is_retryable_ingest_error(KeyError("oops")) is False
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(503, request=req)
    assert (
        is_retryable_ingest_error(httpx.HTTPStatusError("5xx", request=req, response=resp)) is False
    )


# --- enqueue → run against the in-memory connector --------------------------------------------


async def _defer(box: str | None = "case-1", document_id: str | None = None) -> int:
    document_id = document_id or str(uuid.uuid4())
    deferrer = ingest_document_bytes_job.configure(
        queue=f"ingest:{box}" if box else "ingest",
        lock=box,
        queueing_lock=document_id,
    )
    return await deferrer.defer_async(
        document_id=document_id,
        content_b64=base64.b64encode(b"hello world").decode("ascii"),
        media_type="text/plain",
        title="t",
        box=box,
    )


async def _run_worker() -> None:
    await app.run_worker_async(wait=False, install_signal_handlers=False, listen_notify=False)


@pytest.mark.asyncio
async def test_enqueue_then_run_succeeds_and_decodes_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    propositionized: list[uuid.UUID] = []

    async def _fake(**kwargs: object) -> None:
        seen.update(kwargs)

    async def _fake_prop(*, document_id: uuid.UUID) -> None:
        propositionized.append(document_id)

    monkeypatch.setattr("iknos.jobs.app._ingest_one", _fake)
    monkeypatch.setattr("iknos.jobs.app._propositionize_one", _fake_prop)
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        doc = str(uuid.uuid4())
        job_id = await _defer(document_id=doc)
        await _run_worker()  # runs ingest, which chains extraction; the worker drains both
        assert connector.jobs[job_id]["status"] == "succeeded"
    # The task decoded the base64 bytes and passed through the document id / title.
    assert seen["document_bytes"] == b"hello world"
    assert isinstance(seen["document_id"], uuid.UUID)
    assert seen["title"] == "t"
    # A successful ingest chained the follow-on extraction for the same document (queue-scope).
    assert propositionized == [uuid.UUID(doc)]


@pytest.mark.asyncio
async def test_failed_ingest_does_not_chain_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(**kwargs: object) -> None:
        raise DocumentResegmentationError("bad data")

    prop_calls: list[uuid.UUID] = []

    async def _fake_prop(*, document_id: uuid.UUID) -> None:  # pragma: no cover - must not run
        prop_calls.append(document_id)

    monkeypatch.setattr("iknos.jobs.app._ingest_one", _fail)
    monkeypatch.setattr("iknos.jobs.app._propositionize_one", _fake_prop)
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        await _defer()
        await _run_worker()
        # The chain is after the ingest await: a failed ingest never enqueues extraction. Only the
        # one ingest job exists, and extraction was never invoked.
        assert len(connector.jobs) == 1
    assert prop_calls == []


@pytest.mark.asyncio
async def test_propositionize_job_enqueue_then_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[uuid.UUID] = []

    async def _fake_prop(*, document_id: uuid.UUID) -> None:
        seen.append(document_id)

    monkeypatch.setattr("iknos.jobs.app._propositionize_one", _fake_prop)
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        doc = str(uuid.uuid4())
        deferrer = propositionize_document_job.configure(
            queue="ingest:case-1", lock="case-1", queueing_lock=f"propositionize:{doc}"
        )
        job_id = await deferrer.defer_async(document_id=doc, box="case-1")
        await _run_worker()
        assert connector.jobs[job_id]["status"] == "succeeded"
    assert seen == [uuid.UUID(doc)]


@pytest.mark.asyncio
async def test_validation_error_fails_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(**kwargs: object) -> None:
        raise DocumentResegmentationError("bad data")

    monkeypatch.setattr("iknos.jobs.app._ingest_one", _fake)
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        job_id = await _defer()
        await _run_worker()
        job = connector.jobs[job_id]
        assert job["status"] == "failed"  # terminal
        assert job["attempts"] == 1  # not retried


@pytest.mark.asyncio
async def test_transport_error_is_scheduled_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(**kwargs: object) -> None:
        raise httpx.ConnectError("down", request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr("iknos.jobs.app._ingest_one", _fake)
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        job_id = await _defer()
        await _run_worker()  # processes attempt 1; a transport failure schedules a retry
        job = connector.jobs[job_id]
        # Not terminal: it went back to todo (scheduled for a future retry), one attempt spent.
        assert job["status"] == "todo"
        assert job["attempts"] == 1
        assert job["scheduled_at"] is not None  # exponential backoff


@pytest.mark.asyncio
async def test_same_document_queueing_lock_blocks_a_duplicate_enqueue() -> None:
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        doc = str(uuid.uuid4())
        await _defer(document_id=doc)
        # A second enqueue for the same document id (the queueing_lock) while the first is still
        # waiting is rejected — one document is never queued twice concurrently (§6).
        with pytest.raises(Exception):  # noqa: B017 - AlreadyEnqueued
            await _defer(document_id=doc)


# --- R11-H item 2: a failed-spans extraction report must not report `succeeded` ----------------


def test_clean_extraction_report_does_not_raise() -> None:
    # No failed spans → the helper is a no-op and the job completes normally. Duck-typed report
    # (a SimpleNamespace) keeps this pin torch-free — the helper only reads `.failed_spans`.
    clean = SimpleNamespace(action_ids=[uuid.uuid4()], failed_spans=[])
    _raise_for_failed_spans(clean, document_id=uuid.uuid4(), span_count=3)  # no exception


def test_failed_spans_report_re_raises_retryable() -> None:
    # R11-H item 2: propositionize_document isolates per-span errors into report.failed_spans and
    # commits only the spans that succeeded. The worker must fail loud (§6.1/§13) so procrastinate
    # re-fires — otherwise an all-failed extraction commits nothing yet reports `succeeded`.
    failed = SimpleNamespace(action_ids=[], failed_spans=[object(), object()])
    with pytest.raises(IngestRetryableError) as exc_info:
        _raise_for_failed_spans(failed, document_id=uuid.uuid4(), span_count=5)
    # And it is classified retryable, so the re-fire actually happens (up to MAX_ATTEMPTS).
    assert is_retryable_ingest_error(exc_info.value) is True


# --- R11-H item 3: a duplicate chain defer (AlreadyEnqueued) must not fail the ingest -----------


@pytest.mark.asyncio
async def test_chain_defer_already_enqueued_does_not_fail_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake(**kwargs: object) -> None:
        pass

    monkeypatch.setattr("iknos.jobs.app._ingest_one", _fake)
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        doc = str(uuid.uuid4())
        box = "case-1"
        # Hold the chain's queueing_lock: an extraction for this document is already pending (a
        # re-ingest whose earlier chain has not yet run — realistic with the gate runner's stable
        # uuid5 ids).
        await propositionize_document_job.configure(
            queue=f"ingest:{box}", lock=box, queueing_lock=f"propositionize:{doc}"
        ).defer_async(document_id=doc, box=box)
        # Invoke the ingest task body directly (Task.__call__ runs the function) so its chain defer
        # fires against the held lock deterministically. It must swallow AlreadyEnqueued, not raise.
        await ingest_document_bytes_job(
            document_id=doc,
            content_b64=base64.b64encode(b"hi").decode("ascii"),
            media_type="text/plain",
            title="t",
            box=box,
        )
        # The duplicate chain defer was a no-op: still exactly the one pre-existing extraction job.
        assert len(connector.jobs) == 1


def test_retry_budget_matches_spec() -> None:
    assert MAX_ATTEMPTS == 3
