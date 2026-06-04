import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from iknos.core.proposition import (
    Propositionizer,
    build_context,
    build_messages,
    span_text,
)
from iknos.types.nodes import Span


def _span(doc_id: uuid.UUID, start: int, end: int) -> Span:
    return Span(id=uuid.uuid4(), document_id=doc_id, start=start, end=end)


def test_span_text_slices_raw_text():
    doc = uuid.uuid4()
    raw = "Hello world. Goodbye."
    assert span_text(raw, _span(doc, 0, 12)) == "Hello world."


def test_build_context_preceding_window():
    doc = uuid.uuid4()
    raw = "AAAA BBBB CCCC DDDD"
    spans = [_span(doc, 0, 4), _span(doc, 5, 9), _span(doc, 10, 14), _span(doc, 15, 19)]
    # index 3, window 2 -> preceding spans at indices 1 and 2 ("BBBB", "CCCC")
    ctx_spans, ctx_text = build_context(spans, index=3, raw_text=raw, window=2)
    assert [s.start for s in ctx_spans] == [5, 10]
    assert ctx_text == "BBBB\nCCCC"


def test_build_context_start_of_document_is_empty():
    doc = uuid.uuid4()
    raw = "AAAA BBBB"
    spans = [_span(doc, 0, 4), _span(doc, 5, 9)]
    ctx_spans, ctx_text = build_context(spans, index=0, raw_text=raw, window=8)
    assert ctx_spans == []
    assert ctx_text == ""


def test_build_context_window_zero():
    doc = uuid.uuid4()
    raw = "AAAA BBBB CCCC"
    spans = [_span(doc, 0, 4), _span(doc, 5, 9), _span(doc, 10, 14)]
    ctx_spans, ctx_text = build_context(spans, index=2, raw_text=raw, window=0)
    assert ctx_spans == []
    assert ctx_text == ""


def test_build_messages_marks_context_and_target():
    msgs = build_messages("prior text", "target text")
    assert msgs[0]["role"] == "system"
    assert "CONTEXT:\nprior text" in msgs[1]["content"]
    assert "TARGET:\ntarget text" in msgs[1]["content"]


def test_build_messages_no_context_placeholder():
    msgs = build_messages("   ", "target text")
    assert "(no preceding context)" in msgs[1]["content"]


def _propositionizer(llm_return, embed_return):
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(return_value=llm_return)
    substrate = MagicMock()
    substrate.embed_passages = MagicMock(return_value=embed_return)
    return Propositionizer(llm, substrate, context_window=8, concurrency=2)


@pytest.mark.asyncio
async def test_infer_span_maps_propositions_to_target_span():
    doc = uuid.uuid4()
    raw = "Smith spoke. He argued it was insufficient."
    spans = [_span(doc, 0, 12), _span(doc, 13, 44)]
    p = _propositionizer(
        llm_return={
            "propositions": [
                {"text": "Smith argued the budget was insufficient."},
                {"text": "Smith made an argument."},
            ]
        },
        embed_return=[[1.0, 0.0], [0.0, 1.0]],
    )

    results = await p._infer_span(spans, index=1, raw_text=raw)

    assert [r.text for r in results] == [
        "Smith argued the budget was insufficient.",
        "Smith made an argument.",
    ]
    # Every proposition is evidenced by the target span (index 1), not the context span.
    assert {r.span_id for r in results} == {spans[1].id}
    assert all(r.document_id == doc for r in results)
    assert results[0].embedding == [1.0, 0.0]

    # The context window (the preceding span) was passed to the LLM for resolution.
    sent_messages = p.llm.guided_complete.call_args.args[0]
    assert "Smith spoke." in sent_messages[1]["content"]


@pytest.mark.asyncio
async def test_infer_span_empty_returns_no_results_and_skips_embedding():
    doc = uuid.uuid4()
    raw = "Well, anyway."
    spans = [_span(doc, 0, 13)]
    p = _propositionizer(llm_return={"propositions": []}, embed_return=[])

    results = await p._infer_span(spans, index=0, raw_text=raw)

    assert results == []
    p.substrate.embed_passages.assert_not_called()
