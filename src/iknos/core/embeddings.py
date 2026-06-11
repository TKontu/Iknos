from typing import Any

import torch
from transformers import AutoModel, AutoTokenizer

# BAAI/bge-m3 context window (special tokens included). A single model forward pass
# cannot see more than this many tokens at once.
MAX_MODEL_TOKENS = 8192

# G1.13 slice 2 — windowed embedding ("late chunking over windows"). A document longer
# than one model context is embedded as a sequence of overlapping macro-windows, each a
# full forward pass over a slice of the document's *content* tokens (re-framed with the
# model's own special tokens). A span pools from the single window where it sits furthest
# from a window edge — maximal bilateral context — never averaged across windows. This
# fixed token overlap guarantees every span has at least one window that contains it with
# context on both sides. It is a **constant, not config**: it is a correctness-bearing
# policy folded into the segmentation content hash (a change re-segments), not a tuning
# knob. (Slice 2 supersedes slice 1's fail-loud ``DocumentTooLongError`` ceiling: there is
# no length a windowed pass cannot cover, so the refusal is gone.)
WINDOW_OVERLAP_TOKENS = 1024


class EmbeddingModelMismatchError(Exception):
    """Dense rows for one document/proposition-set already exist under a *different* model (G1.16).

    Cosine similarity across two embedding spaces is meaningless, so a single ANN index must hold
    vectors from exactly one model. Swapping or upgrading the embedding model and re-ingesting in
    place would silently mix spaces — undetectable, since both models may share a dimension. This
    refuses that write loudly; the migration path is ``scripts/reembed.py`` (re-embed every row to
    the target model first). Mirrors the fail-loud placement of
    ``core/ingest.py::DocumentResegmentationError`` (review A5).
    """


def _plan_windows(num_tokens: int, *, window_size: int, overlap: int) -> list[tuple[int, int]]:
    """Tile ``[0, num_tokens)`` into overlapping ``[start, end)`` token windows. Pure (no torch).

    Stride is ``window_size - overlap``. The final window is anchored to end exactly at
    ``num_tokens`` (so it is full-size whenever the document has at least ``window_size``
    tokens), which keeps a span near the document tail interior to a full window rather than
    stranded in a short tail window. A document that fits in one window yields exactly
    ``[(0, num_tokens)]`` — the single-window path, byte-identical to the pre-windowing
    computation (one forward pass over the whole document).

    Unit-testable without the model, exactly like the old slice-1 truncation guard was.
    """
    if num_tokens <= 0:
        return []
    if num_tokens <= window_size:
        return [(0, num_tokens)]
    if overlap >= window_size:
        raise ValueError(f"window overlap {overlap} must be smaller than window size {window_size}")

    stride = window_size - overlap
    plans: list[tuple[int, int]] = []
    start = 0
    while True:
        end = start + window_size
        if end >= num_tokens:
            # Anchor the last window to the document end so it stays full-size.
            plans.append((num_tokens - window_size, num_tokens))
            break
        plans.append((start, end))
        start += stride
    return plans


def mean_pool_normalize(
    token_embeddings: torch.Tensor, attention_mask: torch.Tensor
) -> list[list[float]]:
    """Mask-aware mean pool + L2 normalize for a batch of passages.

    token_embeddings: (batch, seq_len, hidden). attention_mask: (batch, seq_len),
    1 for real tokens, 0 for padding. Returns one normalized vector per passage.
    Padded positions are excluded so the result is independent of batch padding.
    """
    mask = attention_mask.unsqueeze(-1).to(token_embeddings.dtype)  # (batch, seq, 1)
    summed = (token_embeddings * mask).sum(dim=1)  # (batch, hidden)
    counts = mask.sum(dim=1).clamp(min=1.0)  # (batch, 1) — avoid div-by-zero
    pooled = summed / counts
    pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
    return pooled.tolist()


class _Window:
    """One embedded macro-window: contextualized token embeddings + their char offsets.

    ``token_embeddings`` is ``(1, win_len, hidden)`` over the window's **content** tokens
    only (special tokens stripped); ``offset_mapping`` is the aligned ``(start_char, end_char)``
    per content token, into the *document* text. ``char_start``/``char_end`` are the window's
    char coverage, used by :meth:`DocumentContext.pool_span` to pick the most-interior window.
    """

    __slots__ = ("token_embeddings", "offset_mapping", "char_start", "char_end")

    def __init__(self, token_embeddings: torch.Tensor, offset_mapping: list[tuple[int, int]]):
        self.token_embeddings = token_embeddings
        self.offset_mapping = offset_mapping
        content = [(s, e) for (s, e) in offset_mapping if not (s == e == 0)]
        self.char_start = min((s for s, _ in content), default=0)
        self.char_end = max((e for _, e in content), default=0)

    def overlapping_token_indices(self, start_char: int, end_char: int) -> list[int]:
        out = []
        for i, (tok_start, tok_end) in enumerate(self.offset_mapping):
            if tok_start == tok_end == 0:
                # Special tokens like [CLS]/[SEP] (only present on the legacy single-window
                # constructor; the windowing path strips them).
                continue
            if tok_start < end_char and tok_end > start_char:
                out.append(i)
        return out


class DocumentContext:
    """Cached contextualized token embeddings for one document, held as 1+ macro-windows.

    Built once per document by :meth:`EmbeddingSubstrate.embed_document`; every span/sentence
    granularity is pooled from it (late chunking — embed once, derive all levels). A document
    that fits the model context is a single window; a longer one is a sequence of overlapping
    windows (G1.13 slice 2). The public single-window constructor keeps the byte-identical
    pre-windowing path (and existing direct-construction tests) working.
    """

    def __init__(
        self,
        token_embeddings: torch.Tensor,
        offset_mapping: list[tuple[int, int]],
        *,
        windowing: dict[str, Any] | None = None,
    ):
        # Single-window construction (legacy + the n==1 case): one window holding the whole
        # document's token embeddings, special tokens included (pool_span skips them).
        self._windows = [_Window(token_embeddings, offset_mapping)]
        self._windowing = windowing or {
            "overlap": WINDOW_OVERLAP_TOKENS,
            "model_max_tokens": MAX_MODEL_TOKENS,
            "window_token_size": MAX_MODEL_TOKENS,
        }

    @classmethod
    def from_windows(
        cls,
        windows: list[tuple[torch.Tensor, list[tuple[int, int]]]],
        *,
        windowing: dict[str, Any],
    ) -> "DocumentContext":
        """Build a (possibly multi-window) context from ``(token_embeddings, offsets)`` pairs."""
        self = cls.__new__(cls)
        self._windows = [_Window(te, om) for te, om in windows]
        self._windowing = windowing
        return self

    @property
    def _hidden(self) -> int:
        return self._windows[0].token_embeddings.shape[-1]

    def windowing_policy(self) -> dict[str, Any]:
        """The **policy** that produced this context (overlap / model max / window size).

        Stable inputs only — no data-dependent window count or boundaries — so it folds into
        ``ingest.span_content_hash``: a changed windowing policy re-segments instead of silently
        reusing spans embedded under the old policy.
        """
        return dict(self._windowing)

    def window_layout(self) -> dict[str, Any]:
        """The full window layout for the segment ``Action`` audit (policy + count + boundaries)."""
        return {
            **self._windowing,
            "count": len(self._windows),
            "boundaries": [[w.char_start, w.char_end] for w in self._windows],
        }

    def pool_span(self, start_char: int, end_char: int) -> list[float]:
        """Pool the token embeddings overlapping ``[start_char, end_char)`` into one vector.

        With multiple windows, the span is pooled from the single window where it sits
        **furthest from a window edge** — i.e. the window maximizing
        ``min(start - win_start, win_end - end)`` among windows that actually contain tokens
        of the span. That window gives the span the most bilateral context, and (since adjacent
        sentences are tiny relative to the token overlap) makes two adjacent sentences select the
        *same* interior window, so the segmentation backbone's adjacent-sentence cosine compares
        embeddings from one consistent context — the "values from the window where both positions
        are interior" rule, realized through per-span selection rather than a separate code path.
        Never averages across windows. A single-window context is the degenerate case and is
        byte-identical to the pre-windowing computation.
        """
        best_score: int | None = None
        best: tuple[_Window, list[int]] | None = None
        for w in self._windows:
            idx = w.overlapping_token_indices(start_char, end_char)
            if not idx:
                continue
            score = min(start_char - w.char_start, w.char_end - end_char)
            if best_score is None or score > best_score:
                best_score = score
                best = (w, idx)

        if best is None:
            # No window has a token overlapping the span (e.g. a whitespace-only span).
            return [0.0] * self._hidden

        window, token_indices = best
        span_embeddings = window.token_embeddings[0, token_indices, :]
        pooled = span_embeddings.mean(dim=0)
        # Normalize (bge-m3 uses cosine similarity).
        pooled = torch.nn.functional.normalize(pooled, p=2, dim=0)
        return pooled.tolist()


class EmbeddingSubstrate:
    def __init__(self, model_name_or_path: str = "BAAI/bge-m3", device: str | None = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        # Self-describing: the model identity feeds the segmentation content hash and
        # the Action audit row (core/ingest.py), so consumers don't re-specify it.
        self.model_name = model_name_or_path
        self.tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
        self.model = AutoModel.from_pretrained(model_name_or_path).to(self.device)
        self.model.eval()

    def embed_document(self, text: str) -> DocumentContext:
        """Embed the document into a :class:`DocumentContext` of one or more macro-windows.

        Tokenizes the whole document **once without truncation** (content tokens only), tiles it
        into overlapping windows (:func:`_plan_windows`), and runs one model forward pass per
        window — each window re-framed with the model's own special tokens so interior windows
        are properly bracketed. The contextualized embeddings of the window's content tokens are
        kept (special-token positions stripped) and mapped back to their document char offsets.

        A document that fits the context window is a single window whose pooled vectors are
        byte-identical to the pre-windowing path; a longer one is covered in full (G1.13 slice 2),
        replacing the slice-1 fail-loud refusal — no span is ever silently dropped from the dense
        index.
        """
        enc = self.tokenizer(
            text, add_special_tokens=False, return_offsets_mapping=True, return_tensors="pt"
        )
        content_ids = enc["input_ids"][0]
        content_offsets = [(int(s), int(e)) for s, e in enc["offset_mapping"][0].tolist()]
        num_content = int(content_ids.shape[0])

        num_special = self.tokenizer.num_special_tokens_to_add(pair=False)
        window_size = MAX_MODEL_TOKENS - num_special
        policy = {
            "overlap": WINDOW_OVERLAP_TOKENS,
            "model_max_tokens": MAX_MODEL_TOKENS,
            "window_token_size": window_size,
        }

        plans = _plan_windows(num_content, window_size=window_size, overlap=WINDOW_OVERLAP_TOKENS)
        windows: list[tuple[torch.Tensor, list[tuple[int, int]]]] = []
        for tok_start, tok_end in plans:
            win_ids = content_ids[tok_start:tok_end].tolist()
            model_ids = self.tokenizer.build_inputs_with_special_tokens(win_ids)
            special_mask = self.tokenizer.get_special_tokens_mask(
                win_ids, already_has_special_tokens=False
            )
            content_pos = [i for i, m in enumerate(special_mask) if m == 0]

            input_ids = torch.tensor([model_ids], device=self.device)
            attention_mask = torch.ones_like(input_ids)
            with torch.no_grad():
                outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)
                hidden = outputs.last_hidden_state[0].cpu()  # (len(model_ids), hidden)

            win_emb = hidden[content_pos].unsqueeze(0)  # (1, win_len, hidden)
            windows.append((win_emb, content_offsets[tok_start:tok_end]))

        if not windows:
            # Empty / token-less document: one empty window so pool_span returns the
            # zero-vector fallback (which ingest skips) rather than indexing nothing.
            hidden_size = int(self.model.config.hidden_size)
            windows = [(torch.zeros((1, 0, hidden_size)), [])]

        return DocumentContext.from_windows(windows, windowing=policy)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embed standalone short passages to one normalized 1024-d vector each.

        Distinct from embed_document/pool_span: propositions are rewritten text
        that does not appear in the source document, so they cannot be pooled from
        cached document context and are embedded afresh here. Real batching: one
        padded tokenizer call + one forward pass, then mask-aware mean pool.
        """
        if not texts:
            return []

        inputs = self.tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=MAX_MODEL_TOKENS,
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            token_embeddings = outputs.last_hidden_state

        return mean_pool_normalize(token_embeddings.cpu(), inputs["attention_mask"].cpu())
