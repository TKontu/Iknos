"""The shared baseline output contract + question schema (Trial A0 / V4–V6).

Every rung of the E1 baseline ladder — plain RAG (V4), agentic RAG (V5), and the expert+search
protocol (V6) — produces the **same** answer shape, so the V3 harness scores the whole ladder
identically and no rung gets a scoring advantage from its output format. This module is that
contract and its TOML serialization. It is pure standard library (``tomllib`` to read; a tiny
hand-rolled writer to emit) — no model, no DB, importable with ``DATABASE_URL`` unset.

:class:`BaselineAnswer` is the per-question output; :class:`BaselineQuestion` is the per-question
input (the question text + which differentiator axis it probes). Gold answers are **not** here —
they are Trial V2, and the baselines/experts must not see them (the contamination rule).
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# The differentiator axes E1 scores (architecture.md §8). A question declares which one it
# probes so results can be read per-axis (RAG is expected to be weakest on contradiction
# handling, retraction, and traceability). "factoid" is the easy-tie control.
AXES = frozenset(
    {
        "root_cause",
        "retraction",
        "refuter",
        "traceability",
        "entity_resolution",
        "governance",
        "factoid",
    }
)


@dataclass(frozen=True)
class BaselineQuestion:
    """One question put to every rung of the ladder.

    ``axis`` is one of :data:`AXES` — the differentiator dimension the question targets. No gold
    answer lives here (that is V2); the field set is deliberately answer-free so the file is safe
    to hand to a V6 expert or an E1 operator without contaminating them.
    """

    id: str
    text: str
    axis: str

    def __post_init__(self) -> None:
        if self.axis not in AXES:
            raise ValueError(f"question {self.id!r} has unknown axis {self.axis!r}")


@dataclass(frozen=True)
class BaselineAnswer:
    """One rung's answer to one question — the shared scoring contract.

    * ``question_id`` — the :class:`BaselineQuestion` it answers.
    * ``answer_text`` — the free-text answer.
    * ``cited_chunk_ids`` — the retrieval-chunk ids (or, for the expert rung, passage anchors)
      the answer relies on. The traceability axis scores what the rung can cite, so this must be
      complete, not decorative.
    * ``confidence`` — the rung's own ``[0, 1]`` confidence. For the LLM rungs this is the
      **verbalized** confidence (the baseline's own calibration story — deliberately *not*
      multi-sampled; that is the system's differentiator, not the baseline's).
    """

    question_id: str
    answer_text: str
    cited_chunk_ids: tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence for {self.question_id!r} is outside [0, 1]: {self.confidence}"
            )


@dataclass(frozen=True)
class UnansweredQuestion:
    """A question a rung could not answer (e.g. a malformed agentic tool call after one retry).

    Recorded **loudly** rather than dropped: a silently missing answer would read as a corpus of
    fewer questions and bias the score. Serialized into the same file under ``[[unanswered]]``.
    """

    question_id: str
    reason: str


def load_questions(path: Path) -> list[BaselineQuestion]:
    """Load a questions TOML (``[[questions]]`` with ``id`` / ``text`` / ``axis``)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    questions = [
        BaselineQuestion(id=str(q["id"]), text=str(q["text"]), axis=str(q["axis"]))
        for q in data.get("questions", [])
    ]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate question ids in questions file")
    return questions


# --- TOML emission (a tiny, dependency-free writer; we only emit strings/floats/lists) ---


def _toml_str(value: str) -> str:
    """A TOML basic-string literal: escape backslash, quote, and control chars."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _toml_str_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


@dataclass
class AnswerFile:
    """An on-disk baseline answer set: the metadata header + answers + unanswered questions.

    Written to ``docs/trials/baseline_<rung>_answers.toml`` by the runner and read back by the
    V3 scoring harness. ``meta`` records how the run was produced (rung, corpus, models) so a
    score is reproducible and attributable.
    """

    meta: dict[str, str]
    answers: Sequence[BaselineAnswer]
    unanswered: Sequence[UnansweredQuestion] = field(default_factory=tuple)

    def to_toml(self) -> str:
        lines = ["# Generated by scripts/run_baseline.py — do not edit by hand.", "", "[meta]"]
        for key, value in self.meta.items():
            lines.append(f"{key} = {_toml_str(value)}")
        for ans in self.answers:
            lines += [
                "",
                "[[answers]]",
                f"question_id = {_toml_str(ans.question_id)}",
                f"answer_text = {_toml_str(ans.answer_text)}",
                f"cited_chunk_ids = {_toml_str_list(ans.cited_chunk_ids)}",
                f"confidence = {ans.confidence}",
            ]
        for un in self.unanswered:
            lines += [
                "",
                "[[unanswered]]",
                f"question_id = {_toml_str(un.question_id)}",
                f"reason = {_toml_str(un.reason)}",
            ]
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_toml(), encoding="utf-8")


def load_answers(path: Path) -> AnswerFile:
    """Read back an :class:`AnswerFile` (the V3 harness's entry point to a rung's results)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    answers = [
        BaselineAnswer(
            question_id=str(a["question_id"]),
            answer_text=str(a["answer_text"]),
            cited_chunk_ids=tuple(str(c) for c in a.get("cited_chunk_ids", [])),
            confidence=float(a["confidence"]),
        )
        for a in data.get("answers", [])
    ]
    unanswered = [
        UnansweredQuestion(question_id=str(u["question_id"]), reason=str(u["reason"]))
        for u in data.get("unanswered", [])
    ]
    return AnswerFile(
        meta={k: str(v) for k, v in data.get("meta", {}).items()},
        answers=answers,
        unanswered=unanswered,
    )
