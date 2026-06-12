"""Fixed-size chunk boundaries (``iknos.baselines.chunking``) — verified with a whitespace fake.

The production tokenizer is bge-m3's; here a whitespace ``PassageTokenizer`` makes one token per
word, so the windowing arithmetic (stride, overlap, the final-window break, exact-substring text)
is checked deterministically with no model loaded.
"""

from __future__ import annotations

import re

import pytest

from iknos.baselines.chunking import Chunk, chunk_document


class WhitespaceTokenizer:
    """One token per whitespace-delimited word, with real char offsets."""

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]


TOK = WhitespaceTokenizer()


def test_overlapping_windows() -> None:
    # "a b c d e": chunk 3 / overlap 1 / stride 2 -> [a b c], [c d e]; c is the shared overlap.
    chunks = chunk_document("doc", "a b c d e", TOK, chunk_tokens=3, overlap_tokens=1)
    assert [c.text for c in chunks] == ["a b c", "c d e"]
    assert [c.index for c in chunks] == [0, 1]


def test_chunk_text_is_exact_substring() -> None:
    text = "alpha beta gamma delta epsilon"
    for chunk in chunk_document("doc", text, TOK, chunk_tokens=2, overlap_tokens=0):
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_document_shorter_than_one_chunk_is_one_chunk() -> None:
    chunks = chunk_document("doc", "a b", TOK, chunk_tokens=5, overlap_tokens=1)
    assert chunks == [Chunk(document_id="doc", index=0, char_start=0, char_end=3, text="a b")]


def test_final_window_does_not_duplicate_the_tail() -> None:
    # 5 tokens, chunk 2 / overlap 0 / stride 2 -> [0:2],[2:4],[4:5]; the last is the 1-token tail,
    # emitted once (the loop breaks when a window reaches the end), never as an empty extra window.
    chunks = chunk_document("doc", "a b c d e", TOK, chunk_tokens=2, overlap_tokens=0)
    assert [c.text for c in chunks] == ["a b", "c d", "e"]


def test_empty_document_yields_no_chunks() -> None:
    assert chunk_document("doc", "   ", TOK, chunk_tokens=4, overlap_tokens=0) == []


def test_default_overlap_is_smaller_than_default_chunk() -> None:
    # A long run exercises the default 512/64 policy boundaries without asserting exact text.
    text = " ".join(f"w{i}" for i in range(1300))
    chunks = chunk_document("doc", text, TOK)  # defaults: 512 / 64, stride 448
    assert [c.index for c in chunks] == [0, 1, 2]  # 0:512, 448:960, 896:1300
    assert chunks[-1].char_end == len(text)


def test_invalid_chunk_size_raises() -> None:
    with pytest.raises(ValueError):
        chunk_document("doc", "a b", TOK, chunk_tokens=0)


def test_overlap_not_smaller_than_chunk_raises() -> None:
    with pytest.raises(ValueError):
        chunk_document("doc", "a b", TOK, chunk_tokens=3, overlap_tokens=3)
