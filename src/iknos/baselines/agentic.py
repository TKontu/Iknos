"""Agentic / multi-hop RAG baseline (Trial A0 / V5) — the strongest cheap competitor.

The second rung of the E1 ladder: an LLM-driven loop over two tools — ``search(query)`` (V4's
cosine retrieval) and ``answer(text, citations, confidence)`` — with query reformulation and
multiple searches allowed, ending in an answer. It is the strongest baseline a competent team
builds *without* this project's reasoning, so beating it is the real E1 bar.

Built **directly on guided-JSON structured output** (``core.llm``) — no agent-framework
dependency (principle 7). Each step is one guided call returning either a ``search`` or an
``answer`` action; the rig executes searches against the V4 retriever, accumulates the seen
chunks into a numbered pool the model cites from, and stops at the first ``answer`` or a hard
step budget. It emits the **same** :class:`~iknos.baselines.contract.BaselineAnswer` as the
other rungs, plus a complete :class:`~iknos.baselines.contract.QuestionTrace` (every query
issued, every chunk seen) — E1's traceability axis scores what the baseline can cite, so the
trace must be complete, not decorative.

Boundary: it gets the retriever and the LLM, and **nothing** of iknos's graph / propositions /
contradiction machinery (enforced by ``tests/unit/test_baselines_import_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from iknos.baselines.contract import (
    BaselineAnswer,
    BaselineQuestion,
    QuestionTrace,
    UnansweredQuestion,
)
from iknos.baselines.rag import ANSWER_SCHEMA, RetrievedChunk, parse_answer

# Budget (V5 spec): up to MAX_SEARCH_STEPS decision calls, then one forced answer call if the
# model never chose to answer — so at most MAX_SEARCH_STEPS + 1 LLM calls per question.
MAX_SEARCH_STEPS = 6

# One flat guided schema for a step: an action plus the fields each action needs. Guided JSON
# cannot easily express "query required iff action == search", so the conditional requirements
# are validated in code (:func:`_validate_step`); a step that fails validation is a malformed
# tool call (one retry, then the question is recorded unanswered — loudly).
STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"enum": ["search", "answer"]},
        "query": {"type": "string", "description": "The search query (when action is 'search')."},
        "answer_text": {"type": "string", "description": "The answer (when action is 'answer')."},
        "cited_chunks": {"type": "array", "items": {"type": "integer"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["action"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a research assistant answering a QUESTION over a document corpus you can only see "
    'through search. On each turn choose ONE action as JSON: either {"action": "search", '
    '"query": ...} to retrieve more excerpts, or {"action": "answer", "answer_text": ..., '
    '"cited_chunks": [...], "confidence": ...} to finish. You may search several times with '
    f"reformulated queries (up to {MAX_SEARCH_STEPS}), but must finish with an answer. Answer "
    "using ONLY the excerpts you have seen; cite the excerpt numbers you relied on; give a "
    "calibrated confidence in [0, 1]. If the excerpts do not contain the answer, answer plainly "
    "and say so."
)


class Retriever(Protocol):
    """The search tool: the V4 ``RagBaseline.retrieve`` (top-k cosine over ``baseline_chunks``)."""

    async def retrieve(self, question: str) -> list[RetrievedChunk]: ...


class GuidedLLM(Protocol):
    async def guided_complete(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        sampling: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgenticResult:
    """One question's outcome: an answer **or** an unanswered marker, always with the full trace."""

    trace: QuestionTrace
    answer: BaselineAnswer | None = None
    unanswered: UnansweredQuestion | None = None


def _render_pool(seen: Sequence[RetrievedChunk]) -> str:
    if not seen:
        return "(no excerpts retrieved yet)"
    return "\n\n".join(f"[{i}] {c.text.strip()}" for i, c in enumerate(seen, start=1))


def build_step_messages(
    question: str, queries: Sequence[str], seen: Sequence[RetrievedChunk]
) -> list[dict[str, str]]:
    """The messages for one loop step: the question, the queries already tried, the seen pool."""
    tried = ", ".join(f'"{q}"' for q in queries) if queries else "(none yet)"
    user = (
        f"QUESTION:\n{question}\n\n"
        f"QUERIES ALREADY TRIED: {tried}\n\n"
        f"EXCERPTS SEEN SO FAR:\n{_render_pool(seen)}"
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def build_final_answer_messages(
    question: str, seen: Sequence[RetrievedChunk]
) -> list[dict[str, str]]:
    """The forced final answer prompt when the step budget is exhausted without an answer."""
    user = (
        f"QUESTION:\n{question}\n\nEXCERPTS SEEN:\n{_render_pool(seen)}\n\n"
        "You have used your search budget. Answer now using only the excerpts above; cite the "
        "excerpt numbers you relied on and give a calibrated confidence in [0, 1]."
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _validate_step(raw: dict[str, Any]) -> str | None:
    """Return an error string if the step is malformed, else ``None``.

    Guided decoding guarantees the JSON is schema-valid; this checks the *semantic* requirements
    the flat schema cannot express — a search needs a non-empty query, an answer needs answer
    text and a confidence.
    """
    action = raw.get("action")
    if action == "search":
        if not str(raw.get("query", "")).strip():
            return "search action with empty query"
        return None
    if action == "answer":
        if not str(raw.get("answer_text", "")).strip():
            return "answer action with empty answer_text"
        if "confidence" not in raw:
            return "answer action without confidence"
        return None
    return f"unknown action {action!r}"


class AgenticRagBaseline:
    """The multi-hop RAG rung: an LLM-driven search→answer loop over the V4 retriever."""

    def __init__(
        self,
        *,
        retriever: Retriever,
        llm: GuidedLLM,
        max_search_steps: int = MAX_SEARCH_STEPS,
        sampling: dict[str, Any] | None = None,
    ) -> None:
        self._retriever = retriever
        self._llm = llm
        self._max_search_steps = max_search_steps
        self._sampling = sampling

    async def _step_call(self, messages: list[dict[str, str]]) -> dict[str, Any] | None:
        """One step decision with a single retry on a malformed (semantically invalid) call."""
        for _ in range(2):  # the initial call + one retry
            raw = await self._llm.guided_complete(messages, STEP_SCHEMA, self._sampling)
            if _validate_step(raw) is None:
                return raw
        return None

    async def answer(self, question: BaselineQuestion) -> AgenticResult:
        """Run the search→answer loop for one question; return an answer or unanswered, + trace."""
        seen: list[RetrievedChunk] = []
        seen_ids: set[str] = set()
        queries: list[str] = []

        def trace() -> QuestionTrace:
            return QuestionTrace(
                question_id=question.id,
                queries=tuple(queries),
                seen_chunk_ids=tuple(c.id for c in seen),
            )

        for _ in range(self._max_search_steps):
            raw = await self._step_call(build_step_messages(question.text, queries, seen))
            if raw is None:
                return AgenticResult(
                    trace=trace(),
                    unanswered=UnansweredQuestion(question.id, "malformed tool call after retry"),
                )
            if raw["action"] == "answer":
                return AgenticResult(trace=trace(), answer=parse_answer(question.id, raw, seen))
            # action == "search": run it, accumulate unique chunks into the numbered pool.
            query = str(raw["query"]).strip()
            queries.append(query)
            for chunk in await self._retriever.retrieve(query):
                if chunk.id not in seen_ids:
                    seen_ids.add(chunk.id)
                    seen.append(chunk)

        # Budget exhausted without an answer: one forced final answer call (the "+1").
        raw = await self._llm.guided_complete(
            build_final_answer_messages(question.text, seen), ANSWER_SCHEMA, self._sampling
        )
        return AgenticResult(trace=trace(), answer=parse_answer(question.id, raw, seen))
