"""Gate-corpus dry-run ingest — the first real multi-document corpus ingest (Trial C / §6.1).

Ingests all ten `tests/fixtures/gate_corpus/` documents through the **real ingest pipeline via the
R11 Postgres job queue** (procrastinate) and the **R10 embedding seam**, then sanity-reads the
result and writes a run report under `docs/trials/`. This is the event R10 (out-of-process
embedding) and R11 (per-box serialized job queue) exist for: a single investigation's documents
ingested concurrently-but-serialized, exactly as a live operator would drive them.

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
    distinct_embedding_windows: int = 0  # rows with span_id NULL = document-level windows (d08)
    propositions: int = 0
    faithfulness_present: int = 0  # propositions with a non-null faithfulness (verifier ran)
    provisional: int = 0


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
    coverage is read from ``document_embeddings`` (the R10 dense rows), where a row with ``span_id``
    NULL is a document-level embedding *window* — d08 must show more than one (the G1.13 multi-
    window fact lives in its tail).
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from iknos.config import settings
    from iknos.db.age import bootstrap_session, execute_cypher, unquote_agtype

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
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
                window_rows = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM document_embeddings "
                            "WHERE document_id = :did AND span_id IS NULL"
                        ),
                        {"did": doc.document_id},
                    )
                ).scalar_one()
                c.distinct_embedding_windows = int(window_rows)
                results.append(c)
    finally:
        await engine.dispose()
    return results


def _actions_have_metrics() -> bool:
    """Whether R12 (Action cost/duration metrics) has landed — its columns on the Action model.

    Checked reflectively so this runner needs no edit when R12 merges: if the columns appear, the
    report can be extended to read them. Today they are absent, so the report says so.
    """
    from iknos.db.orm import Action

    cols = set(Action.__table__.columns.keys())
    return bool(cols & {"duration_ms", "cost", "duration", "latency_ms"})


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
    d08 = next((c for c in counts if c.key == "d08"), None)
    lines = [
        "# Gate-corpus dry-run ingest — run report (Trial C / §6.1)",
        "",
        f"- corpus: `{corpus_dir}` ({len(docs)} documents); box: `{box}`; "
        f"graph: `{settings.graph_name}`",
        f"- embedding seam (R10): `{settings.embeddings_base_url or 'in-process bge-m3'}`; "
        f"model: `{settings.embedding_model}`",
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
        "- R12 Action cost/duration metrics: "
        + (
            "**available**"
            if _actions_have_metrics()
            else "**not merged** — per-document "
            "cost/duration omitted (the §6.1 / Trial C numbers land once R12 ships)"
        ),
        "",
        "## Per-document",
        "",
        "| doc | spans | embedding rows by level | doc windows | propositions | "
        "faithfulness set | provisional |",
        "|-----|------:|-------------------------|------------:|-------------:|"
        "-----------------:|------------:|",
    ]
    for c in counts:
        by_level = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.embedding_rows_by_level.items()))
        lines.append(
            f"| {c.key} | {c.spans} | {by_level or '—'} | {c.distinct_embedding_windows} "
            f"| {c.propositions} | {c.faithfulness_present} | {c.provisional} |"
        )
    lines += ["", "## d08 multi-window check (G1.13)", ""]
    if d08 is None:
        lines.append("- d08 not found in the corpus — cannot verify the tail-window-coverage fact.")
    elif d08.distinct_embedding_windows > 1:
        lines.append(
            f"- ✅ d08 produced **{d08.distinct_embedding_windows}** document-level embedding "
            "windows — the >8,192-token purchasing record spans more than one window, so the "
            "load-bearing tail fact in its final 10% is covered (the path R10 exists to serve)."
        )
    else:
        lines.append(
            f"- ⚠️ d08 produced only **{d08.distinct_embedding_windows}** document-level window — "
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
