"""Plain-RAG rig logic (``iknos.baselines.rag``) — prompt assembly, answer parsing, orchestration.

The DB-backed ingest/retrieve path is exercised by the integration test (live AGE+pgvector);
here the pure pieces are tested directly, and ``answer()`` is driven with a fake retrieve + a
mock LLM so the build→call→parse glue is covered without a database or a model.
"""

from __future__ import annotations

import uuid

import pytest

from iknos.baselines.contract import BaselineAnswer, BaselineQuestion
from iknos.baselines.rag import (
    ANSWER_SCHEMA,
    RagBaseline,
    RetrievedChunk,
    baseline_document_uuid,
    build_answer_messages,
    parse_answer,
)


def _chunk(chunk_id: str, text: str, distance: float = 0.1) -> RetrievedChunk:
    return RetrievedChunk(
        id=chunk_id,
        document_id="doc",
        chunk_index=0,
        char_start=0,
        char_end=len(text),
        text=text,
        distance=distance,
    )


# --- baseline_document_uuid ---


def test_document_uuid_is_deterministic_and_distinct() -> None:
    assert baseline_document_uuid("d01") == baseline_document_uuid("d01")
    assert baseline_document_uuid("d01") != baseline_document_uuid("d02")
    assert isinstance(baseline_document_uuid("d01"), uuid.UUID)


# --- build_answer_messages ---


def test_messages_number_excerpts_and_carry_question() -> None:
    msgs = build_answer_messages(
        "Why did it fail?", [_chunk("a", "Oil was degraded."), _chunk("b", "No alarm.")]
    )
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "Why did it fail?" in user
    assert "[1] Oil was degraded." in user
    assert "[2] No alarm." in user


def test_messages_handle_no_excerpts() -> None:
    msgs = build_answer_messages("Q?", [])
    assert "(no excerpts retrieved)" in msgs[1]["content"]


# --- parse_answer: citation-number -> chunk-id mapping ---


def test_parse_maps_citations_to_chunk_ids() -> None:
    presented = [_chunk("id-a", "A"), _chunk("id-b", "B"), _chunk("id-c", "C")]
    raw = {"answer_text": "Because A and C.", "cited_chunks": [1, 3], "confidence": 0.7}
    ans = parse_answer("q1", raw, presented)
    assert ans == BaselineAnswer(
        question_id="q1",
        answer_text="Because A and C.",
        cited_chunk_ids=("id-a", "id-c"),
        confidence=0.7,
    )


def test_parse_drops_out_of_range_and_dedupes_citations() -> None:
    presented = [_chunk("id-a", "A"), _chunk("id-b", "B")]
    raw = {"answer_text": "x", "cited_chunks": [1, 1, 5, 0], "confidence": 0.5}
    ans = parse_answer("q1", raw, presented)
    assert ans.cited_chunk_ids == ("id-a",)  # 1 once; 5 and 0 are out of range


def test_parse_clamps_confidence() -> None:
    presented = [_chunk("id-a", "A")]
    assert (
        parse_answer(
            "q", {"answer_text": "x", "cited_chunks": [], "confidence": 1.4}, presented
        ).confidence
        == 1.0
    )
    assert (
        parse_answer(
            "q", {"answer_text": "x", "cited_chunks": [], "confidence": -0.2}, presented
        ).confidence
        == 0.0
    )


def test_answer_schema_is_closed() -> None:
    assert ANSWER_SCHEMA["additionalProperties"] is False
    assert set(ANSWER_SCHEMA["required"]) == {"answer_text", "cited_chunks", "confidence"}


# --- answer(): orchestration with a fake retrieve + mock LLM ---


class _MockLLM:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[list[dict[str, str]], dict]] = []

    async def guided_complete(self, messages, json_schema, sampling=None):  # type: ignore[no-untyped-def]
        self.calls.append((messages, json_schema))
        return self.response


class _NullEmbedder:
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


def _rig(llm: _MockLLM) -> RagBaseline:
    return RagBaseline(
        embedder=_NullEmbedder(),
        llm=llm,
        session_factory=lambda: (_ for _ in ()).throw(AssertionError("no DB in this test")),  # type: ignore[arg-type,return-value]
        tokenizer=type("T", (), {"offsets": lambda self, text: []})(),
        model_name="BAAI/bge-m3",
    )


@pytest.mark.asyncio
async def test_answer_orchestrates_retrieve_build_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _MockLLM({"answer_text": "Lubrication failure.", "cited_chunks": [2], "confidence": 0.8})
    rig = _rig(llm)
    canned = [_chunk("id-x", "irrelevant"), _chunk("id-y", "the oil was degraded")]

    async def fake_retrieve(question: str) -> list[RetrievedChunk]:
        return canned

    monkeypatch.setattr(rig, "retrieve", fake_retrieve)
    ans = await rig.answer(BaselineQuestion(id="q01", text="root cause?", axis="root_cause"))

    assert ans.question_id == "q01"
    assert ans.answer_text == "Lubrication failure."
    assert ans.cited_chunk_ids == ("id-y",)  # excerpt [2] -> the second presented chunk
    assert ans.confidence == 0.8
    # The LLM saw the numbered excerpts and the closed schema.
    ((messages, schema),) = llm.calls
    assert "[2] the oil was degraded" in messages[1]["content"]
    assert schema is ANSWER_SCHEMA


def test_sampling_defaults_to_pinned_greedy_and_is_overridable() -> None:
    # V12: every other LLM consumer pins temperature 0.0; the baseline must too (reproducibility),
    # while still allowing an explicit regime.
    from iknos.baselines.rag import DEFAULT_SAMPLING

    assert DEFAULT_SAMPLING == {"temperature": 0.0}
    assert _rig(_MockLLM({}))._sampling == {"temperature": 0.0}
    explicit = RagBaseline(
        embedder=_NullEmbedder(),
        llm=_MockLLM({}),
        session_factory=lambda: (_ for _ in ()).throw(AssertionError("no DB")),  # type: ignore[arg-type,return-value]
        tokenizer=type("T", (), {"offsets": lambda self, text: []})(),
        model_name="m",
        sampling={"temperature": 0.7},
    )
    assert explicit._sampling == {"temperature": 0.7}
