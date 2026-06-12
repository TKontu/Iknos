"""The shared baseline contract + the gate questions file (``iknos.baselines.contract``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from iknos.baselines.contract import (
    AXES,
    AnswerFile,
    BaselineAnswer,
    BaselineQuestion,
    QuestionTrace,
    UnansweredQuestion,
    load_answers,
    load_questions,
)

GATE_QUESTIONS = Path(__file__).parent.parent / "fixtures" / "gate_corpus" / "questions.toml"


# --- validation ---


def test_answer_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        BaselineAnswer("q", "a", (), 1.5)


def test_question_rejects_unknown_axis() -> None:
    with pytest.raises(ValueError):
        BaselineQuestion("q", "text", "not_an_axis")


# --- the gate questions file ---


def test_gate_questions_load_with_valid_axes_and_unique_ids() -> None:
    questions = load_questions(GATE_QUESTIONS)
    assert len(questions) >= 10
    assert len({q.id for q in questions}) == len(questions)
    assert all(q.axis in AXES for q in questions)


def test_gate_questions_cover_the_differentiator_axes() -> None:
    # E1's whole point is the axes where RAG is weak; assert the question set actually exercises
    # contradiction/retraction, refuters, traceability and entity resolution (not just factoids).
    axes = {q.axis for q in load_questions(GATE_QUESTIONS)}
    for required in ("retraction", "refuter", "traceability", "entity_resolution"):
        assert required in axes, f"gate questions miss the {required} axis"


# --- AnswerFile round-trip ---


def test_answer_file_roundtrips_through_toml(tmp_path: Path) -> None:
    original = AnswerFile(
        meta={"baseline": "rag", "llm_model": "test-model"},
        answers=[
            BaselineAnswer("q01", 'He said "it was the oil".', ("c1", "c2"), 0.8),
            BaselineAnswer("q02", "Multi\nline\tanswer.", (), 0.0),
        ],
        unanswered=[UnansweredQuestion("q03", "malformed tool call after retry")],
    )
    path = tmp_path / "answers.toml"
    original.write(path)
    reloaded = load_answers(path)

    assert reloaded.meta == original.meta
    assert list(reloaded.answers) == list(original.answers)
    assert list(reloaded.unanswered) == list(original.unanswered)


def test_answer_file_roundtrips_traces(tmp_path: Path) -> None:
    # The agentic rung's per-question trace (queries + seen chunks) must survive serialization.
    original = AnswerFile(
        meta={"baseline": "agentic"},
        answers=[BaselineAnswer("q01", "ans", ("c1",), 0.7)],
        traces=[QuestionTrace("q01", ("first query", "second query"), ("c1", "c2", "c3"))],
    )
    path = tmp_path / "agentic.toml"
    original.write(path)
    assert list(load_answers(path).traces) == list(original.traces)


def test_answer_file_escapes_special_characters(tmp_path: Path) -> None:
    # Quotes, backslashes, newlines and tabs in answer text must survive the hand-rolled writer.
    answer = BaselineAnswer("q", 'a "quote", a \\ backslash, a\nnewline', (), 0.5)
    path = tmp_path / "a.toml"
    AnswerFile(meta={}, answers=[answer]).write(path)
    assert load_answers(path).answers[0].answer_text == answer.answer_text
