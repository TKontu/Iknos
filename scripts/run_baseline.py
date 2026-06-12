"""CLI runner for the E1 baseline ladder (Trial A0 / V4–V5).

Ingests a corpus into the chosen baseline rig, answers a questions TOML, and writes the shared
:class:`~iknos.baselines.contract.AnswerFile` the V3 harness scores. The rig logic lives in
``iknos.baselines`` (importable, type-checked, unit-tested); this is the thin entry point that
wires the real embedding substrate, LLM client, and DB engine.

Usage::

    uv run python -m scripts.run_baseline --baseline rag \\
        --corpus tests/fixtures/gate_corpus \\
        --questions tests/fixtures/gate_corpus/questions.toml \\
        --output docs/trials/baseline_rag_answers.toml

The corpus directory must hold a ``manifest.toml`` with a ``[[documents]]`` table (``id`` +
``filename``) — the same schema the gate corpus uses. Requires ``DATABASE_URL`` and the LLM
endpoint to be configured (the rig ingests into ``baseline_chunks`` and calls the LLM).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import tomllib
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from iknos.baselines.agentic import AgenticRagBaseline
from iknos.baselines.chunking import SubstrateTokenizer
from iknos.baselines.contract import (
    AnswerFile,
    BaselineAnswer,
    QuestionTrace,
    UnansweredQuestion,
    load_questions,
)
from iknos.baselines.rag import RagBaseline
from iknos.config import settings
from iknos.core.embeddings import EmbeddingSubstrate
from iknos.core.llm import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_OUTPUTS = {
    "rag": "docs/trials/baseline_rag_answers.toml",
    "agentic": "docs/trials/baseline_agentic_answers.toml",
}


def _load_corpus_documents(corpus_dir: Path) -> list[tuple[str, str]]:
    """Return ``(document_id, text)`` for each ``[[documents]]`` entry in the corpus manifest."""
    manifest = tomllib.loads((corpus_dir / "manifest.toml").read_text(encoding="utf-8"))
    docs: list[tuple[str, str]] = []
    for entry in manifest["documents"]:
        text = (corpus_dir / entry["filename"]).read_text(encoding="utf-8")
        docs.append((str(entry["id"]), text))
    return docs


async def _run_baseline(args: argparse.Namespace) -> AnswerFile:
    """Ingest the corpus once (shared), then answer every question with the chosen rung."""
    corpus_dir = Path(args.corpus)
    documents = _load_corpus_documents(corpus_dir)
    questions = load_questions(Path(args.questions))

    substrate = EmbeddingSubstrate(args.embedding_model)
    engine = create_async_engine(settings.database_url)
    session_local = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    llm = LLMClient(model=args.llm_model)
    # The sampling regime is pinned (greedy by default) and recorded in meta so a score is
    # reproducible and attributable (V12): without this the baseline's confidences drifted run to
    # run and the answers file did not say under what regime they were produced.
    sampling: dict[str, Any] = {"temperature": args.temperature}
    answers: list[BaselineAnswer] = []
    unanswered: list[UnansweredQuestion] = []
    traces: list[QuestionTrace] = []
    try:
        rig = RagBaseline(
            embedder=substrate,
            llm=llm,
            session_factory=session_local,
            tokenizer=SubstrateTokenizer(substrate.tokenizer),
            model_name=substrate.model_name,
            top_k=args.top_k,
            chunk_tokens=args.chunk_tokens,
            overlap_tokens=args.overlap_tokens,
            sampling=sampling,
        )
        for doc_id, text in documents:
            n = await rig.ingest_document(doc_id, text)
            logger.info("ingested %s -> %d chunks", doc_id, n)

        if args.baseline == "rag":
            for q in questions:
                answers.append(await rig.answer(q))
                logger.info("answered %s", q.id)
        else:  # "agentic" — the multi-hop loop over the same retriever
            agentic = AgenticRagBaseline(
                retriever=rig, llm=llm, max_search_steps=args.max_steps, sampling=sampling
            )
            for q in questions:
                result = await agentic.answer(q)
                traces.append(result.trace)
                if result.answer is not None:
                    answers.append(result.answer)
                    logger.info("answered %s (%d queries)", q.id, len(result.trace.queries))
                if result.unanswered is not None:
                    unanswered.append(result.unanswered)
                    logger.warning(
                        "UNANSWERED %s (%d queries): %s",
                        q.id,
                        len(result.trace.queries),
                        result.unanswered.reason,
                    )
    finally:
        substrate.close()
        await engine.dispose()

    meta = {
        "baseline": args.baseline,
        "corpus": str(corpus_dir),
        "embedding_model": substrate.model_name,
        "llm_model": llm.model,
        "top_k": str(args.top_k),
        "chunk_tokens": str(args.chunk_tokens),
        "overlap_tokens": str(args.overlap_tokens),
        "sampling": json.dumps(sampling, sort_keys=True),
    }
    if args.baseline == "agentic":
        meta["max_steps"] = str(args.max_steps)
    return AnswerFile(meta=meta, answers=answers, unanswered=unanswered, traces=traces)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an E1 baseline over a corpus + questions.")
    parser.add_argument("--baseline", required=True, choices=["rag", "agentic"])
    parser.add_argument("--corpus", required=True, help="Corpus dir with manifest.toml.")
    parser.add_argument("--questions", required=True, help="Questions TOML.")
    parser.add_argument(
        "--output", default=None, help="Output answers TOML (rig default if unset)."
    )
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument("--llm-model", default=None, help="Served LLM id (default: LLM_MODEL env).")
    parser.add_argument("--top-k", type=int, default=8, help="Chunks retrieved per question.")
    parser.add_argument("--chunk-tokens", type=int, default=512)
    parser.add_argument("--overlap-tokens", type=int, default=64)
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="LLM sampling temperature (default 0.0 = greedy, pinned for reproducibility).",
    )
    parser.add_argument(
        "--max-steps", type=int, default=6, help="Agentic: max search steps before a forced answer."
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=settings.log_level)
    answer_file = await _run_baseline(args)
    output = Path(args.output or DEFAULT_OUTPUTS[args.baseline])
    answer_file.write(output)
    logger.info(
        "wrote %d answers (%d unanswered) to %s",
        len(answer_file.answers),
        len(answer_file.unanswered),
        output,
    )


if __name__ == "__main__":
    asyncio.run(main())
