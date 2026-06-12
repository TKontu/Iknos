"""Fixed-size token chunking for the plain-RAG baseline (Trial A0 / V4).

**Deliberately not iknos segmentation.** The whole point of the E1 baseline is to retrieve over
naive fixed-size chunks — what a competent team builds without this project — so the system's
proposition/segmentation layer has something to beat. This module windows a document into
overlapping fixed-token chunks (default 512 tokens / 64 overlap) and nothing more.

It is pure given a :class:`PassageTokenizer` — a minimal seam exposing just the per-token char
offsets the windowing needs. The production tokenizer is the embedding substrate's own (so chunk
boundaries land on the same tokenization the vectors are built from); tests pass a whitespace
fake, so the boundary logic is verified with no model loaded. Chunk text is sliced from the
original document by char offset, so every chunk is an **exact substring** — citations point at
real text (the traceability axis).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

DEFAULT_CHUNK_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64


class PassageTokenizer(Protocol):
    """The minimal tokenizer seam the chunker needs: per-content-token ``(start, end)`` offsets.

    Implemented for production by :class:`SubstrateTokenizer` (wrapping the bge-m3 tokenizer with
    ``add_special_tokens=False, return_offsets_mapping=True`` — the same call the embedding
    substrate uses), and by a whitespace fake in the unit tests.
    """

    def offsets(self, text: str) -> list[tuple[int, int]]: ...


@dataclass(frozen=True)
class Chunk:
    """One fixed-size chunk: the source document id, its ordinal, char span, and exact text.

    ``index`` is the chunk's position in the document (0-based), so a document's chunks have a
    stable, reproducible identity. ``char_start``/``char_end`` make the chunk traceable back to
    the document; ``text`` is ``document[char_start:char_end]`` (an exact substring).
    """

    document_id: str
    index: int
    char_start: int
    char_end: int
    text: str


def chunk_document(
    document_id: str,
    text: str,
    tokenizer: PassageTokenizer,
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Window ``text`` into overlapping fixed-token chunks.

    The stride is ``chunk_tokens - overlap_tokens``; each chunk covers token range
    ``[start, start + chunk_tokens)`` and its char span runs from the first token's start offset
    to the last token's end offset, so adjacent chunks overlap by ``overlap_tokens`` tokens of
    shared context. A document with no tokens yields no chunks; one shorter than ``chunk_tokens``
    yields a single chunk covering it.
    """
    if chunk_tokens <= 0:
        raise ValueError(f"chunk_tokens must be positive, got {chunk_tokens}")
    if not (0 <= overlap_tokens < chunk_tokens):
        raise ValueError(f"overlap_tokens must be in [0, {chunk_tokens}), got {overlap_tokens}")
    offsets = tokenizer.offsets(text)
    if not offsets:
        return []
    stride = chunk_tokens - overlap_tokens
    chunks: list[Chunk] = []
    index = 0
    tok_start = 0
    n = len(offsets)
    while tok_start < n:
        tok_end = min(tok_start + chunk_tokens, n)
        char_start = offsets[tok_start][0]
        char_end = offsets[tok_end - 1][1]
        chunks.append(
            Chunk(
                document_id=document_id,
                index=index,
                char_start=char_start,
                char_end=char_end,
                text=text[char_start:char_end],
            )
        )
        index += 1
        if tok_end == n:  # the final window reached the end; do not emit a redundant tail
            break
        tok_start += stride
    return chunks


class SubstrateTokenizer:
    """Production :class:`PassageTokenizer` wrapping the embedding substrate's tokenizer.

    Uses ``add_special_tokens=False, return_offsets_mapping=True`` — exactly the call
    ``EmbeddingSubstrate.embed_document`` uses — so chunk boundaries align with the tokenization
    the embeddings are built from. Imported lazily-safe: holds the already-loaded tokenizer, never
    loads a model itself.
    """

    def __init__(self, tokenizer: object) -> None:
        self._tokenizer = tokenizer

    def offsets(self, text: str) -> list[tuple[int, int]]:
        enc = self._tokenizer(  # type: ignore[operator]
            text, add_special_tokens=False, return_offsets_mapping=True
        )
        mapping: Sequence[Sequence[int]] = enc["offset_mapping"]
        return [(int(s), int(e)) for s, e in mapping]
