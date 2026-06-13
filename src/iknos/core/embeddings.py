from typing import Any, Protocol, runtime_checkable

import torch
from transformers import AutoModel, AutoTokenizer

# BAAI/bge-m3 context window (special tokens included). A single model forward pass
# cannot see more than this many tokens at once.
MAX_MODEL_TOKENS = 8192

# bge-m3 dense dimension. The pgvector columns are `vector(1024)`; an out-of-process backend
# (R10) validates every returned vector against this, so a wrong-dimension server response is
# rejected at the trust boundary rather than failing later at the DB write.
EMBEDDING_DIM = 1024

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

    def pool_span(self, start_char: int, end_char: int) -> list[float] | None:
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

        Returns ``None`` when the span overlaps no token in any window (e.g. a whitespace-only
        span) — **not** a zero vector (G1.17, review R3). A zero vector is a meaningless point in
        cosine space that poisons the ANN index; ``None`` makes "no embedding" explicit so callers
        skip the span rather than relying on every one of them to recognize the sentinel. The
        invariant downstream is that no zero vector ever reaches pgvector.
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
            # No window has a token overlapping the span (e.g. a whitespace-only span): no
            # embedding exists for it. Return None (not a zero-vector sentinel) so callers skip.
            return None

        window, token_indices = best
        span_embeddings = window.token_embeddings[0, token_indices, :]
        pooled = span_embeddings.mean(dim=0)
        # Normalize (bge-m3 uses cosine similarity).
        pooled = torch.nn.functional.normalize(pooled, p=2, dim=0)
        return pooled.tolist()


def _common_prefix_len(xs: list[int], ys: list[int]) -> int:
    """Length of the longest shared leading run of two id sequences."""
    n = 0
    for x, y in zip(xs, ys, strict=False):
        if x != y:
            break
        n += 1
    return n


def _derive_special_affixes(tokenizer: Any) -> tuple[list[int], list[int]]:
    """The special-token ids the tokenizer wraps a single sequence with, as ``(prefix, suffix)``.

    ``embed_document`` tokenizes the whole document once without special tokens, tiles it, then
    must re-frame each window's content ids with the model's special tokens. Older transformers
    exposed ``build_inputs_with_special_tokens`` / ``get_special_tokens_mask`` on the (slow)
    tokenizer for exactly this; **transformers>=5 removed both from the fast tokenizers** AGE/bge-m3
    loads (``AutoTokenizer`` returns a fast ``XLMRobertaTokenizer`` whose ``__getattr__`` now raises
    on those names, and ``get_special_tokens_mask`` raises ``NotImplementedError`` for non-formatted
    input). So we recover the wrapping from probes — encode content with and without special tokens
    and diff — using only ``__call__``, which is stable across transformers versions. For bge-m3
    (XLM-RoBERTa) this yields ``([bos], [eos]) == ([0], [2])``; a tokenizer that adds no special
    tokens yields ``([], [])``.

    **Two-probe recovery (W12).** The wrapping is content-independent, so the prefix is the longest
    token run shared at the *front* of two different probes' wrapped encodings and the suffix the
    longest run shared at their *back*. This is robust even when a content token id equals a leading
    special id (e.g. a tokenizer whose ``"a"`` encodes to ``[bos]``): the old single-probe
    left-to-right substring search mislocated the prefix there, returning ``([], [bos, eos])``
    instead of ``([bos], [eos])``. The two probes must differ at both content ends so the shared
    run stops exactly at the content boundary; if they happen to collide there (degenerate
    tokenizer) we fall back to the substring search, which is correct whenever no content id equals
    a special id — the production bge-m3 case the four originally-shipped tests cover.
    """
    content_a = list(tokenizer("a", add_special_tokens=False)["input_ids"])
    if not content_a:  # pathological tokenizer (empty content encoding) — assume no wrapping
        return [], []
    wrapped_a = list(tokenizer("a", add_special_tokens=True)["input_ids"])
    n = len(content_a)
    k = len(wrapped_a) - n  # total special tokens wrapping a single sequence
    if k <= 0:
        return [], []  # no wrapping, or the tokenizer rewrote the content — nothing to recover

    content_b = list(tokenizer("0", add_special_tokens=False)["input_ids"])
    if content_b and content_b[0] != content_a[0] and content_b[-1] != content_a[-1]:
        # Probes differ at both ends → the shared front/back runs are exactly the special affixes.
        wrapped_b = list(tokenizer("0", add_special_tokens=True)["input_ids"])
        prefix_len = _common_prefix_len(wrapped_a, wrapped_b)
        suffix_len = _common_prefix_len(wrapped_a[::-1], wrapped_b[::-1])
        if prefix_len + suffix_len == k and wrapped_a[prefix_len : prefix_len + n] == content_a:
            return wrapped_a[:prefix_len], wrapped_a[prefix_len + n :]

    # Fallback: locate the content run verbatim. Correct whenever no content id collides with a
    # leading special id (the production case); the two-probe path above covers the collision.
    for i in range(len(wrapped_a) - n + 1):
        if wrapped_a[i : i + n] == content_a:
            return wrapped_a[:i], wrapped_a[i + n :]
    return [], []  # content not found verbatim (tokenizer rewrote it) — fall back to no wrapping


class EmbeddingSubstrate:
    """Wraps the long-context embedding model (late chunking — embed once, derive all levels).

    **Lifecycle (G1.17 R6).** Loading the model is the expensive part — seconds and gigabytes of
    (GPU) memory — so a long-running worker constructs **one** substrate and holds it for its
    lifetime, embedding every document through it; it does **not** build one per document. Use
    :meth:`close` (or the context-manager form ``with EmbeddingSubstrate(...) as s:``) to release
    the model + tokenizer at shutdown, or in a test/CLI that spins up many. ``close`` is idempotent;
    on CUDA it also frees the allocator's cached blocks.
    """

    def __init__(self, model_name_or_path: str = "BAAI/bge-m3", device: str | None = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        # Self-describing: the model identity feeds the segmentation content hash and
        # the Action audit row (core/ingest.py), so consumers don't re-specify it.
        self.model_name = model_name_or_path
        self.tokenizer: Any = AutoTokenizer.from_pretrained(model_name_or_path)
        # The model's special-token wrapping, derived once (see _derive_special_affixes): the
        # transformers>=5 fast tokenizer no longer exposes build_inputs_with_special_tokens.
        self._special_prefix, self._special_suffix = _derive_special_affixes(self.tokenizer)
        self.model: Any = AutoModel.from_pretrained(model_name_or_path).to(self.device)
        self.model.eval()

    def close(self) -> None:
        """Release the model + tokenizer; free CUDA cache on GPU. Idempotent (G1.17 R6).

        After ``close`` the substrate must not embed again (the references are dropped so Python
        can reclaim the model memory). Safe to call more than once.
        """
        self.model = None
        self.tokenizer = None
        if self.device.startswith("cuda"):
            torch.cuda.empty_cache()

    def __enter__(self) -> "EmbeddingSubstrate":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

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

        num_special = len(self._special_prefix) + len(self._special_suffix)
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
            # Re-frame the window with the model's special tokens; content sits contiguously
            # between the derived prefix/suffix, so its positions are an exact slice (no need for
            # the removed get_special_tokens_mask).
            model_ids = self._special_prefix + win_ids + self._special_suffix
            content_pos = slice(len(self._special_prefix), len(self._special_prefix) + len(win_ids))

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


@runtime_checkable
class EmbeddingBackend(Protocol):
    """The swappable embedding seam (R10): document context + passage vectors + model identity.

    :class:`EmbeddingSubstrate` (in-process torch) is the **default/local** backend and already
    satisfies this Protocol structurally; :class:`~iknos.core.embeddings_http.HTTPEmbeddingBackend`
    is the out-of-process one, behind ``EMBEDDINGS_BASE_URL`` (the same service edge as the LLM and
    parser). Both are **synchronous** — ``core/ingest.py`` calls ``embed_document`` inline — so the
    HTTP backend uses a sync client, not an async one, to stay drop-in interchangeable.

    ``model_name`` is the served model's identity; it feeds the segmentation content hash and the
    G1.16 vector-space guard (one ANN index, one model), so a backend must report the model it
    actually produced the vectors with.
    """

    @property
    def model_name(self) -> str: ...

    def embed_document(self, text: str) -> DocumentContext: ...

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    def close(self) -> None:
        """Release backend resources (torch tensors / the HTTP client). Idempotent.

        Part of the seam so the ingest worker can construct a backend through
        :func:`make_embedding_backend` and close it without knowing which concrete backend it
        got — both :class:`EmbeddingSubstrate` and ``HTTPEmbeddingBackend`` implement it.
        """
        ...


def make_embedding_backend(
    *,
    base_url: str | None = None,
    model_name: str | None = None,
    timeout_s: float | None = None,
    device: str | None = None,
) -> EmbeddingBackend:
    """The single construction point for an embedding backend (R10) — the ingest worker's seam.

    An **empty base URL is the "no service" signal** → the in-process :class:`EmbeddingSubstrate`
    (byte-identical to before; torch in the worker). A non-empty ``EMBEDDINGS_BASE_URL`` routes
    embedding to the hosted service via :class:`~iknos.core.embeddings_http.HTTPEmbeddingBackend`,
    so torch need not live in the worker. Mirrors ``core/parse.py::make_parser``: defaults are read
    from config only when not supplied (a caller passing everything stays DB-free).
    """
    # Only consult the config singleton (which requires DATABASE_URL) when a default is needed.
    if base_url is None or model_name is None:
        from iknos.config import settings

        base_url = settings.embeddings_base_url if base_url is None else base_url
        model_name = settings.embedding_model if model_name is None else model_name

    if not base_url:
        return EmbeddingSubstrate(model_name, device=device)

    # Lazy import keeps the httpx/pydantic dependency (and any import cycle) off the local path.
    from iknos.core.embeddings_http import HTTPEmbeddingBackend

    return HTTPEmbeddingBackend(base_url=base_url, model_name=model_name, timeout_s=timeout_s)
