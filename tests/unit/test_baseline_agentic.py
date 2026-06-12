"""The agentic / multi-hop RAG loop (``iknos.baselines.agentic``).

Drives the loop with a scripted LLM (a queue of step responses) and a fake retriever, so the
control flow — multi-hop search, cumulative numbered pool, the answer stop, the malformed-call
retry, and the forced final answer at the budget — is verified with no model and no DB.
"""

from __future__ import annotations

import pytest

from iknos.baselines.agentic import (
    MAX_SEARCH_STEPS,
    STEP_SCHEMA,
    AgenticRagBaseline,
    _validate_step,
    build_step_messages,
)
from iknos.baselines.contract import BaselineQuestion
from iknos.baselines.rag import ANSWER_SCHEMA, RetrievedChunk

QUESTION = BaselineQuestion(id="q01", text="What caused the failure?", axis="root_cause")


def _chunk(chunk_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        id=chunk_id,
        document_id="d",
        chunk_index=0,
        char_start=0,
        char_end=len(text),
        text=text,
        distance=0.1,
    )


class _ScriptedLLM:
    """Returns queued responses in order; records which schema each call used."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.schemas: list[dict] = []

    async def guided_complete(self, messages, json_schema, sampling=None):  # type: ignore[no-untyped-def]
        self.schemas.append(json_schema)
        return self._responses.pop(0)


class _MapRetriever:
    """Returns canned chunks per query; records the queries it was asked."""

    def __init__(self, by_query: dict[str, list[RetrievedChunk]]) -> None:
        self._by_query = by_query
        self.queries: list[str] = []

    async def retrieve(self, question: str) -> list[RetrievedChunk]:
        self.queries.append(question)
        return self._by_query.get(question, [])


# --- _validate_step (the malformed-call definition) ---


def test_validate_step_accepts_well_formed() -> None:
    assert _validate_step({"action": "search", "query": "x"}) is None
    assert _validate_step({"action": "answer", "answer_text": "a", "confidence": 0.5}) is None


def test_validate_step_flags_malformed() -> None:
    assert _validate_step({"action": "search", "query": "  "}) is not None
    assert _validate_step({"action": "answer", "answer_text": "", "confidence": 0.5}) is not None
    assert _validate_step({"action": "answer", "answer_text": "a"}) is not None  # no confidence
    assert _validate_step({"action": "frobnicate"}) is not None


# --- the loop ---


@pytest.mark.asyncio
async def test_multi_hop_then_answer() -> None:
    retriever = _MapRetriever(
        {
            "lubrication": [_chunk("c1", "oil was degraded")],
            "oil grade": [_chunk("c2", "VG 150 substituted")],
        }
    )
    # search, search (reformulated), then answer citing both accumulated excerpts.
    llm = _ScriptedLLM(
        [
            {"action": "search", "query": "lubrication"},
            {"action": "search", "query": "oil grade"},
            {
                "action": "answer",
                "answer_text": "Lubrication.",
                "cited_chunks": [1, 2],
                "confidence": 0.8,
            },
        ]
    )
    result = await AgenticRagBaseline(retriever=retriever, llm=llm).answer(QUESTION)

    assert result.answer is not None
    assert result.answer.cited_chunk_ids == ("c1", "c2")  # cumulative pool: [1]=c1, [2]=c2
    assert result.answer.confidence == 0.8
    # The trace is complete: both queries and both seen chunks.
    assert result.trace.queries == ("lubrication", "oil grade")
    assert result.trace.seen_chunk_ids == ("c1", "c2")
    assert retriever.queries == ["lubrication", "oil grade"]


@pytest.mark.asyncio
async def test_answer_on_first_step_does_not_search() -> None:
    retriever = _MapRetriever({})
    llm = _ScriptedLLM(
        [{"action": "answer", "answer_text": "x", "cited_chunks": [], "confidence": 0.3}]
    )
    result = await AgenticRagBaseline(retriever=retriever, llm=llm).answer(QUESTION)
    assert result.answer is not None
    assert result.trace.queries == ()
    assert retriever.queries == []


@pytest.mark.asyncio
async def test_malformed_call_retries_once_then_unanswered() -> None:
    retriever = _MapRetriever({})
    # Two malformed responses for the first step -> initial + one retry both fail -> unanswered.
    llm = _ScriptedLLM([{"action": "search", "query": ""}, {"action": "search", "query": "  "}])
    result = await AgenticRagBaseline(retriever=retriever, llm=llm).answer(QUESTION)
    assert result.answer is None
    assert result.unanswered is not None
    assert "malformed" in result.unanswered.reason


@pytest.mark.asyncio
async def test_retry_recovers_from_one_malformed_call() -> None:
    retriever = _MapRetriever({"good": [_chunk("c1", "found it")]})
    llm = _ScriptedLLM(
        [
            {"action": "search", "query": ""},  # malformed
            {"action": "search", "query": "good"},  # retry succeeds
            {"action": "answer", "answer_text": "ok", "cited_chunks": [1], "confidence": 0.6},
        ]
    )
    result = await AgenticRagBaseline(retriever=retriever, llm=llm).answer(QUESTION)
    assert result.answer is not None
    assert result.answer.cited_chunk_ids == ("c1",)


@pytest.mark.asyncio
async def test_forced_answer_when_budget_exhausted() -> None:
    retriever = _MapRetriever({"q": [_chunk("c1", "partial")]})
    # max_search_steps search responses (never answers), then the rig forces a final answer call.
    steps = [{"action": "search", "query": "q"} for _ in range(3)]
    final = {"answer_text": "forced", "cited_chunks": [1], "confidence": 0.4}
    llm = _ScriptedLLM([*steps, final])
    result = await AgenticRagBaseline(retriever=retriever, llm=llm, max_search_steps=3).answer(
        QUESTION
    )

    assert result.answer is not None
    assert result.answer.answer_text == "forced"
    assert result.trace.queries == ("q", "q", "q")
    # The forced call used the answer-only schema, the steps used the step schema.
    assert llm.schemas[-1] is ANSWER_SCHEMA
    assert llm.schemas[0] is STEP_SCHEMA


@pytest.mark.asyncio
async def test_duplicate_chunks_are_pooled_once() -> None:
    # Two searches returning the same chunk id -> the pool numbers it once.
    retriever = _MapRetriever({"a": [_chunk("c1", "same")], "b": [_chunk("c1", "same")]})
    llm = _ScriptedLLM(
        [
            {"action": "search", "query": "a"},
            {"action": "search", "query": "b"},
            {"action": "answer", "answer_text": "x", "cited_chunks": [1], "confidence": 0.5},
        ]
    )
    result = await AgenticRagBaseline(retriever=retriever, llm=llm).answer(QUESTION)
    assert result.answer is not None
    assert result.trace.seen_chunk_ids == ("c1",)


# --- prompt assembly ---


def test_step_messages_show_tried_queries_and_pool() -> None:
    msgs = build_step_messages("Q?", ["first try"], [_chunk("c1", "excerpt one")])
    user = msgs[1]["content"]
    assert "Q?" in user
    assert '"first try"' in user
    assert "[1] excerpt one" in user


def test_default_budget_matches_spec() -> None:
    assert MAX_SEARCH_STEPS == 6
