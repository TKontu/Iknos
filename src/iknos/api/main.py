"""Iknos HTTP API (FastAPI). Health/version plus the R11 document-ingest enqueue endpoints.

Auth is **not** wired yet — that is a Phase 6 entry criterion (§9.1 clearance needs an identity to
filter on). ``POST /documents`` accepts a file, enqueues a background ingest job on the
procrastinate queue (so a real multi-document corpus ingest never runs as a synchronous foreground
request), and returns the job id; ``GET /jobs/{id}`` reports that job's status.
"""

from __future__ import annotations

import base64
import uuid
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from iknos.jobs.app import app as jobs_app
from iknos.jobs.app import ingest_document_bytes_job

app = FastAPI(title="Iknos API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict[str, str]:
    return {"name": "iknos", "version": "0.1.0"}


@app.post("/documents")
async def enqueue_document(
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    box: Annotated[str | None, Form()] = None,
    media_type: Annotated[str, Form()] = "text/plain",
) -> dict[str, str]:
    """Enqueue a background ingest of an uploaded document; return the job id.

    The job is queued **per box** (``ingest:<box>``) with an execution ``lock`` on the box so a
    box's ingests serialize, and a ``queueing_lock`` on the document id so the same document is
    never queued twice concurrently (§6). A fresh document id is minted here; the ingest is
    idempotent on the document's content hash, so a retry re-runs safely.
    """
    document_id = uuid.uuid4()
    content = await file.read()
    queue = f"ingest:{box}" if box else "ingest"
    deferrer = ingest_document_bytes_job.configure(
        queue=queue,
        lock=str(box) if box else None,
        queueing_lock=str(document_id),
    )
    job_id = await deferrer.defer_async(
        document_id=str(document_id),
        content_b64=base64.b64encode(content).decode("ascii"),
        media_type=media_type,
        title=title,
        box=box,
    )
    return {"job_id": str(job_id), "document_id": str(document_id)}


@app.get("/jobs/{job_id}")
async def job_status(job_id: int) -> dict[str, str]:
    """Report a queued ingest job's status (``todo`` / ``doing`` / ``succeeded`` / ``failed`` …)."""
    status = await jobs_app.job_manager.get_job_status_async(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return {"job_id": str(job_id), "status": str(status.value)}
