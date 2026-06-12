# E1 — Expert + search baseline protocol (Trial V6)

The third rung of the E1 baseline ladder (`docs/todo_trials.md`): a human expert answering the
gate questions with ordinary tools — plain files and text search — and **no iknos**. This is the
ceiling a cheap, competent human process reaches, and the iknos system must show material lift
over it on the differentiator axes (contradiction handling, retraction, traceability,
calibration). It is a **protocol, not code**: a person runs it and records answers into
`e1_expert_answers_template.toml`, which the V3 harness scores under the same `BaselineAnswer`
contract as the RAG rungs.

## Who runs it

A competent reader who is **not the developer** — the developer knows the planted answer key, so
their score would be meaningless. Use the **Trial V2 second annotator** or another colleague.
One expert is enough for a baseline; if two are available, run them independently and report both
(do not average — report the range).

> **Hard requirement (contamination):** the expert must **not** have read
> `tests/fixtures/gate_corpus/README.md`, `manifest.toml`, or anything under
> `tests/fixtures/gate_corpus/labels/`. Those are the answer key. The expert works from the
> documents under `tests/fixtures/gate_corpus/documents/` and the questions in
> `tests/fixtures/gate_corpus/questions.toml` only. The question text is deliberately
> answer-free, so it is safe to share.

## Toolset

- The corpus as **plain text files** (`documents/d01_*.txt` … `documents/d10_*.txt`). Open them
  in any editor.
- **Text search only** — `ripgrep` / `grep` / the editor's find. No iknos, no LLM, no embedding
  retrieval, no graph. The point of this rung is what a person finds by reading and searching.
- Scratch notes are fine; the expert may re-read and revise within the time box.

## Procedure

1. Read `questions.toml` once to see the ten questions.
2. For **each** question, in order:
   - Search and read the documents to find the answer. Treat the documents as a real,
     possibly-inconsistent evidence set: some statements conflict, one document corrects an
     earlier one, and one relevant fact sits deep in a long file.
   - **Time-box to ~25 minutes per question.** Record the actual minutes spent. If the box
     expires, record the best answer reached and move on (this is part of the measurement —
     cost matters).
   - Write down: the **answer** (free text); the **passages relied on** (document id + a short
     verbatim quote, one per relied-on passage — this is the traceability record, so be
     complete, not decorative); a **confidence in [0, 1]** (the expert's own calibrated belief
     that the answer is correct given the evidence); and the **minutes** spent.
3. Do **not** go back and change earlier answers after seeing later questions — answer each in
   isolation, as the RAG rungs do, so the comparison is fair.

## What is being measured (for the expert's awareness — not a hint)

The questions probe the axes where naive retrieval is weak. Without spoiling any answer: some
questions turn on whether two documents **agree**, one turns on noticing that a later document
**corrects** an earlier one, one fact is **buried** in a long file, and some ask the expert to
**weigh a source's interest**. The expert should answer what the evidence actually supports — if
the evidence is mixed or a later document overrides an earlier one, say so.

## Recording the answers

Copy `e1_expert_answers_template.toml` to `docs/trials/e1_expert_answers.toml` and fill one
`[[answers]]` block per question. The fields match the shared contract so the V3 harness scores
this rung identically to plain RAG (V4) and agentic RAG (V5):

- `question_id` — from `questions.toml`.
- `answer_text` — the free-text answer.
- `cited_chunk_ids` — the relied-on passages, each as `"<doc_id>: <verbatim quote>"`. (The RAG
  rungs cite chunk ids here; for the expert, passage anchors play the same traceability role.)
- `confidence` — the expert's `[0, 1]` confidence.
- `time_minutes` — minutes spent (the expert rung's cost signal; the RAG rungs record LLM calls).

A question the expert genuinely cannot answer within the box is recorded under `[[unanswered]]`
with a one-line reason — **loudly**, never left out (a missing answer would bias the score).

## After the run

Hand the filled `e1_expert_answers.toml` to whoever runs the V3 scoring (bias-controlled, against
the V2 gold answers — never LLM-as-judge). The expert's answers and the planted answer key are
compared by the harness, not by the expert.
