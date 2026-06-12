"""Gate-corpus dry-run ingest — the first real multi-document corpus ingest (Trial C / §6.1).

Ingests all ten `tests/fixtures/gate_corpus/` documents through the **real ingest pipeline via the
R11 Postgres job queue** (procrastinate), then sanity-reads the result and writes a run report under
`docs/trials/`. This is the event R11 (per-box serialized job queue) exists for: a single
investigation's documents ingested concurrently-but-serialized, exactly as a live operator would
drive them.

**Embedding backend.** The R11 ingest job constructs the **in-process** `EmbeddingSubstrate`
directly (`iknos.jobs.app`); the R10 `make_embedding_backend` out-of-process seam is **not yet wired
into the job**, so this run embeds in-process regardless of `EMBEDDINGS_BASE_URL`. Wiring that seam
into the job is the other lane's task — the report states what the worker actually used.

**Pipeline scope (important).** The R11 ingest job (`iknos.jobs.app.ingest_document_bytes_job`)
runs the *perception* stage: parse → segment → embed → persist `Span` vertices + dense/sparse
index rows. **Propositionization is a separate operator** (`core.proposition.Propositionizer`),
not wired into the queue and not run here — so the proposition / faithfulness / provisional counts
in the sanity-read reflect whatever propositionization has *separately* produced (zero after a pure
ingest). Surfacing that boundary is part of the dry run's purpose; wiring a `propositionize` job +
a document span-reloader is the follow-up the report names.

**Determinism & idempotency.** Each document gets a stable id ``uuid5(DOC_NAMESPACE, doc_key)`` so a
re-run maps to the same rows and the content-hash idempotency in `core.ingest` makes the second run
a no-op. The queue uses a per-box queue (``ingest:<box>``) + an execution ``lock`` on the box (so
the investigation's ingests serialize — one worker, concurrency 1) + a ``queueing_lock`` on the doc
id (so a document is never queued twice), mirroring `api/main.py`.

Usage::

    # validate wiring without a DB / services — lists what it would enqueue, touches nothing:
    uv run python -m scripts.run_gate_ingest --plan

    # the real dry run (needs DATABASE_URL + the embedding backend; see the runbook):
    DATABASE_URL=… uv run python -m scripts.run_gate_ingest \
        --corpus tests/fixtures/gate_corpus --box gate-corpus \
        --out docs/trials/gate_ingest_report.md

See `docs/trials/gate_ingest_runbook.md` for the full reproducible recipe and the environment it
needs. The report is plain markdown to stdout (and ``--out`` if given).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("gate_ingest")

# A fixed namespace so document ids are reproducible across runs without a wall clock (the same
# discipline the C3 harness uses). Any constant UUID works; this one is arbitrary and permanent.
DOC_NAMESPACE = uuid.UUID("6b3f1d2e-0000-5a7c-8b00-9a7e1c0d4f00")


@dataclass
class GateDoc:
    key: str  # the manifest id, e.g. "d01"
    filename: str
    text: str

    @property
    def document_id(self) -> uuid.UUID:
        return uuid.uuid5(DOC_NAMESPACE, self.key)


@dataclass
class DocCounts:
    key: str
    document_id: str
    spans: int = 0
    embedding_rows_by_level: dict[int, int] = field(default_factory=dict)
    # Document-level embedding window count, read from the **segment Action's**
    # ``inputs.windowing.count`` (written by core/ingest.py from
    # ``DocumentContext.window_layout``). NOT from ``document_embeddings`` rows: every dense row the
    # pipeline writes sets ``span_id`` (the ``span_id IS NULL`` population is a documented "future"
    # slot, db/orm.py — never written today), so counting NULL rows was always 0. d08 must show > 1.
    embedding_windows: int = 0
    propositions: int = 0
    faithfulness_present: int = 0  # propositions with a non-null faithfulness (verifier ran)
    provisional: int = 0
    # Total ingest wall-clock for this document, summed across its parse + segment Action
    # ``metrics.duration_ms`` (R12). ``None`` when the ``metrics`` column is absent (pre-R12) — the
    # observability-floor "absent, never zeroed" discipline.
    ingest_duration_ms: int | None = None


def load_documents(corpus_dir: Path) -> list[GateDoc]:
    """Read the ``[[documents]]`` table from the corpus manifest into ordered :class:`GateDoc`s."""
    manifest = tomllib.loads((corpus_dir / "manifest.toml").read_text(encoding="utf-8"))
    docs: list[GateDoc] = []
    for entry in manifest["documents"]:
        text = (corpus_dir / entry["filename"]).read_text(encoding="utf-8")
        docs.append(GateDoc(key=str(entry["id"]), filename=str(entry["filename"]), text=text))
    return docs


async def enqueue_all(box: str, docs: list[GateDoc]) -> list[tuple[str, int]]:
    """Enqueue every document on the per-box queue with the box lock + per-document queueing lock.

    Returns ``(doc_key, job_id)`` pairs. Mirrors ``api/main.py``'s configure/defer exactly so the
    dry run exercises the *production* enqueue path, not a bespoke one.
    """
    from iknos.jobs.app import app, ingest_document_bytes_job

    queue = f"ingest:{box}"
    out: list[tuple[str, int]] = []
    async with app.open_async():
        for doc in docs:
            deferrer = ingest_document_bytes_job.configure(
                queue=queue,
                lock=box,  # box-level execution lock → one document at a time per box (§6)
                queueing_lock=str(doc.document_id),  # never queue the same document twice
            )
            job_id = await deferrer.defer_async(
                document_id=str(doc.document_id),
                content_b64=base64.b64encode(doc.text.encode("utf-8")).decode("ascii"),
                media_type="text/plain",
                title=doc.key,
                box=box,
            )
            logger.info("enqueued %s -> job %s (doc %s)", doc.key, job_id, doc.document_id)
            out.append((doc.key, job_id))
    return out


async def drain(box: str) -> None:
    """Run one worker over the box queue until it is empty, then stop.

    ``wait=False`` makes the worker process the jobs already queued and return rather than block for
    new ones (the one-shot-drain pattern the R11 unit tests use). ``concurrency=1`` + the per-box
    execution lock is the per-box serialization the gate ingest is meant to demonstrate.
    """
    from iknos.jobs.app import app

    async with app.open_async():
        await app.run_worker_async(
            queues=[f"ingest:{box}"],
            wait=False,
            concurrency=1,
            install_signal_handlers=False,
            listen_notify=False,
        )


async def sanity_read(docs: list[GateDoc]) -> list[DocCounts]:
    """Read back per-document counts from the graph (spans/propositions) + relational (embeddings).

    Separate session from the worker's. Graph reads go through the real ``cypher()`` seam; embedding
    row counts come from ``document_embeddings`` (one dense row per persisted ``Span``). The
    document-level **window count** (d08's G1.13 multi-window fact) is read from the **segment
    ``Action``'s** ``inputs.windowing.count`` — the layout the pipeline actually records — not from
    ``span_id IS NULL`` rows (a population the pipeline never writes). Per-document ingest duration
    sums the parse + segment Actions' R12 ``metrics.duration_ms`` when the column is present.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iknos.config import settings
    from iknos.db.age import bootstrap_session, execute_cypher, unquote_agtype

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    have_metrics = _actions_have_metrics()
    results: list[DocCounts] = []
    try:
        async with session_factory() as session:
            await bootstrap_session(session)
            for doc in docs:
                did = str(doc.document_id)
                c = DocCounts(key=doc.key, document_id=did)

                span_rows = await execute_cypher(
                    session, f"MATCH (s:Span {{document_id: '{did}'}}) RETURN count(s)"
                )
                c.spans = int(unquote_agtype(span_rows[0][0])) if span_rows else 0

                prop_rows = await execute_cypher(
                    session,
                    f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(s:Span {{document_id: '{did}'}}) "
                    "RETURN count(p)",
                )
                c.propositions = int(unquote_agtype(prop_rows[0][0])) if prop_rows else 0

                if c.propositions:
                    dist = await execute_cypher(
                        session,
                        f"MATCH (p:Proposition)-[:EVIDENCED_BY]->(s:Span {{document_id: '{did}'}}) "
                        "RETURN count(p.faithfulness), "
                        "count(CASE WHEN p.provisional THEN 1 END)",
                    )
                    if dist:
                        c.faithfulness_present = int(unquote_agtype(dist[0][0]))
                        c.provisional = int(unquote_agtype(dist[0][1]))

                # Embedding rows by level (R10 dense output); span_id NULL == a document window.
                level_rows = (
                    await session.execute(
                        text(
                            "SELECT level, count(*) FROM document_embeddings "
                            "WHERE document_id = :did GROUP BY level ORDER BY level"
                        ),
                        {"did": doc.document_id},
                    )
                ).all()
                c.embedding_rows_by_level = {int(lvl): int(n) for lvl, n in level_rows}

                # Document-level window count (G1.13): the segment Action records the embedding
                # window layout in inputs.windowing ({count, boundaries, policy...}). The window
                # layout is identical across levels, so the newest segment Action for the doc
                # suffices. ``->>'count'`` is NULL (→ 0) if no segment Action carries windowing.
                win_count = (
                    await session.execute(
                        text(
                            "SELECT inputs->'windowing'->>'count' FROM actions "
                            "WHERE actor = 'segmenter' AND action_type = 'segment' "
                            "AND inputs->>'document_id' = :did "
                            "ORDER BY timestamp DESC LIMIT 1"
                        ),
                        {"did": did},
                    )
                ).scalar_one_or_none()
                c.embedding_windows = int(win_count) if win_count is not None else 0

                # Per-document ingest duration from R12 Action metrics: sum metrics.duration_ms over
                # this document's Actions (parse + segment). SUM ignores rows whose metrics omit the
                # key (->> NULL), so no fabricated zero. Omitted entirely when the column is absent.
                if have_metrics:
                    dur = (
                        await session.execute(
                            text(
                                "SELECT COALESCE(SUM((metrics->>'duration_ms')::int), 0) "
                                "FROM actions WHERE inputs->>'document_id' = :did"
                            ),
                            {"did": did},
                        )
                    ).scalar_one()
                    c.ingest_duration_ms = int(dur)
                results.append(c)
    finally:
        await engine.dispose()
    return results


def _actions_have_metrics() -> bool:
    """Whether R12 (Action operational metrics) is present — the ``metrics`` JSONB column.

    R12 (#103) shipped as a single ``actions.metrics`` JSONB column whose **keys** are
    ``duration_ms`` / token counts / span counts — not top-level ``duration_ms``/``cost`` columns
    (the shape this guard wrongly looked for, so it returned ``False`` forever). Checked off the ORM
    reflectively so the runner needs no schema knowledge: if the column is on the model, the report
    reads per-document duration from it.
    """
    from iknos.db.orm import Action

    return "metrics" in Action.__table__.columns


def render_report(
    *,
    box: str,
    corpus_dir: Path,
    docs: list[GateDoc],
    jobs: list[tuple[str, int]],
    counts: list[DocCounts],
) -> str:
    from iknos.config import settings

    total_spans = sum(c.spans for c in counts)
    total_props = sum(c.propositions for c in counts)
    have_metrics = _actions_have_metrics()
    total_duration = sum(c.ingest_duration_ms or 0 for c in counts)
    d08 = next((c for c in counts if c.key == "d08"), None)
    lines = [
        "# Gate-corpus dry-run ingest — run report (Trial C / §6.1)",
        "",
        f"- corpus: `{corpus_dir}` ({len(docs)} documents); box: `{box}`; "
        f"graph: `{settings.graph_name}`",
        # The R11 ingest job constructs the in-process EmbeddingSubstrate directly (jobs/app.py);
        # the R10 make_embedding_backend out-of-process seam is NOT yet wired into the job, so this
        # run embedded in-process regardless of EMBEDDINGS_BASE_URL. Report what ran, not the seam.
        f"- embedding backend: **in-process bge-m3** (`{settings.embedding_model}`) — the R11 "
        "worker builds `EmbeddingSubstrate` in-process; the R10 `make_embedding_backend` "
        f"out-of-process seam is **not yet wired into the ingest job**, so `EMBEDDINGS_BASE_URL` "
        f"(`{settings.embeddings_base_url or 'unset'}`) had no effect on this run",
        f"- queue (R11): `ingest:{box}` — per-box execution lock, one worker (concurrency 1)",
        "",
        "**Pipeline scope.** The R11 ingest job runs perception only (parse → segment → embed → "
        "persist `Span`s + index rows). Propositionization is a separate operator, not wired into "
        "the queue; proposition / faithfulness counts below are whatever it produced separately.",
        "",
        "## Totals",
        "",
        f"- enqueued jobs: **{len(jobs)}**",
        f"- spans (graph `Span` vertices): **{total_spans}**",
        f"- propositions (graph `Proposition` vertices): **{total_props}** "
        + ("" if total_props else "_(0 — propositionization not run; see scope note)_"),
        "- R12 Action metrics: "
        + (
            f"**present** — total ingest wall-clock **{total_duration} ms** "
            "(per-document below, summed parse+segment `metrics.duration_ms`); token/cost keys are "
            "LLM-stage and absent from this perception-only ingest"
            if have_metrics
            else "**absent** — the `actions.metrics` column is not on this DB; per-document "
            "duration omitted"
        ),
        "",
        "## Per-document",
        "",
        "| doc | spans | embedding rows by level | doc windows | ingest ms | propositions | "
        "faithfulness set | provisional |",
        "|-----|------:|-------------------------|------------:|----------:|-------------:|"
        "-----------------:|------------:|",
    ]
    for c in counts:
        by_level = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.embedding_rows_by_level.items()))
        dur = "—" if c.ingest_duration_ms is None else str(c.ingest_duration_ms)
        lines.append(
            f"| {c.key} | {c.spans} | {by_level or '—'} | {c.embedding_windows} | {dur} "
            f"| {c.propositions} | {c.faithfulness_present} | {c.provisional} |"
        )
    lines += ["", "## d08 multi-window check (G1.13)", ""]
    if d08 is None:
        lines.append("- d08 not found in the corpus — cannot verify the tail-window-coverage fact.")
    elif d08.embedding_windows > 1:
        lines.append(
            f"- ✅ d08 segmented under **{d08.embedding_windows}** document-level embedding "
            "windows (from the segment `Action`'s `inputs.windowing.count`) — the >8,192-token "
            "purchasing record spans more than one window, so the load-bearing tail fact in its "
            "final 10% is covered (the path the multi-window embedding exists to serve)."
        )
    else:
        lines.append(
            f"- ⚠️ d08 segmented under only **{d08.embedding_windows}** document-level window — "
            "expected >1 for the >8,192-token document. Check the segmentation/window policy "
            "(this is the G1.13 tail-coverage regression the gate corpus plants for)."
        )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> str:
    corpus_dir = Path(args.corpus)
    docs = load_documents(corpus_dir)

    if args.plan:
        # No DB, no services: just show what would be enqueued. Validates wiring offline.
        out = [
            "# Gate-corpus dry-run ingest — PLAN (no DB touched)",
            "",
            f"- corpus: `{corpus_dir}` ({len(docs)} documents); box: `{args.box}`; "
            f"queue: `ingest:{args.box}`",
            "",
            "| doc | filename | bytes | document_id (uuid5) |",
            "|-----|----------|------:|---------------------|",
        ]
        for d in docs:
            out.append(
                f"| {d.key} | `{d.filename}` | {len(d.text.encode('utf-8'))} | `{d.document_id}` |"
            )
        return "\n".join(out)

    logger.info("enqueueing %d documents on box %s", len(docs), args.box)
    jobs = await enqueue_all(args.box, docs)
    logger.info("draining queue ingest:%s with one worker", args.box)
    await drain(args.box)
    logger.info("sanity-reading counts")
    counts = await sanity_read(docs)
    return render_report(box=args.box, corpus_dir=corpus_dir, docs=docs, jobs=jobs, counts=counts)


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gate-corpus dry-run ingest via the R11 job queue.")
    p.add_argument("--corpus", default="tests/fixtures/gate_corpus", help="Corpus dir w/ manifest.")
    p.add_argument("--box", default="gate-corpus", help="Investigation box (queue + lock key).")
    p.add_argument("--out", default=None, help="Also write the markdown report here.")
    p.add_argument(
        "--plan",
        action="store_true",
        help="Offline: list what would be enqueued (deterministic ids), touch no DB or service.",
    )
    return p.parse_args()


async def main() -> None:
    args = _parse()
    if args.plan:
        # Offline mode constructs no config singleton, so it runs with no DATABASE_URL / .env.
        logging.basicConfig(level="WARNING")
    else:
        from iknos.config import settings

        logging.basicConfig(level=settings.log_level)
    report = await run(args)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
