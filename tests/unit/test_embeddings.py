from unittest.mock import MagicMock

import pytest
import torch

import iknos.core.embeddings as emb
from iknos.core.embeddings import (
    DocumentContext,
    EmbeddingSubstrate,
    _derive_special_affixes,
    _plan_windows,
    mean_pool_normalize,
)

# --- special-token affix derivation (transformers>=5 fast-tokenizer compat) ---


def _fake_tokenizer(prefix: list[int], suffix: list[int], content: list[int]):
    """A tokenizer stub whose ``__call__`` wraps content ids with ``prefix``/``suffix`` when
    ``add_special_tokens`` (the only behaviour ``_derive_special_affixes`` probes)."""

    def call(_text: str, add_special_tokens: bool = True, **_kw: object) -> dict[str, list[int]]:
        ids = list(content)
        if add_special_tokens:
            ids = prefix + ids + suffix
        return {"input_ids": ids}

    return call


def test_derive_special_affixes_bos_eos_wrapping() -> None:
    # XLM-RoBERTa / bge-m3 shape: a single bos prefix and eos suffix around the content.
    tok = _fake_tokenizer(prefix=[0], suffix=[2], content=[101])
    assert _derive_special_affixes(tok) == ([0], [2])


def test_derive_special_affixes_multi_token_affixes() -> None:
    # A tokenizer that adds more than one special token per side is recovered intact.
    tok = _fake_tokenizer(prefix=[101, 5], suffix=[102], content=[42, 43])
    assert _derive_special_affixes(tok) == ([101, 5], [102])


def test_derive_special_affixes_no_special_tokens() -> None:
    # A tokenizer that adds nothing yields empty affixes (the no-wrapping fallback).
    tok = _fake_tokenizer(prefix=[], suffix=[], content=[7])
    assert _derive_special_affixes(tok) == ([], [])


def test_derive_special_affixes_empty_probe_is_safe() -> None:
    # A pathological tokenizer that encodes the probe to nothing must not raise.
    tok = _fake_tokenizer(prefix=[0], suffix=[2], content=[])
    assert _derive_special_affixes(tok) == ([], [])


def _fake_tokenizer_by_text(
    prefix: list[int], suffix: list[int], content_by_text: dict[str, list[int]]
):
    """A tokenizer stub whose content ids depend on the *text* — so the two distinct probes
    (``"a"`` and ``"0"``) the two-probe recovery uses encode differently, as a real tokenizer's
    would. Wraps with ``prefix``/``suffix`` when ``add_special_tokens``."""

    def call(text: str, add_special_tokens: bool = True, **_kw: object) -> dict[str, list[int]]:
        ids = list(content_by_text[text])
        if add_special_tokens:
            ids = prefix + ids + suffix
        return {"input_ids": ids}

    return call


def test_derive_special_affixes_probe_id_collides_with_special_id() -> None:
    # W12: the probe "a" encodes to a content id equal to the bos prefix id (0). The old
    # single-probe left-to-right search mislocated the prefix (it matched the bos at index 0 and
    # returned ([], [0, 2])); the two-probe common-affix recovery isolates ([0], [2]) correctly.
    tok = _fake_tokenizer_by_text(prefix=[0], suffix=[2], content_by_text={"a": [0], "0": [5]})
    assert _derive_special_affixes(tok) == ([0], [2])


def test_derive_special_affixes_multi_token_probe() -> None:
    # The probe "a" encodes to *multiple* content tokens; the wrapping is still isolated. The
    # second probe "0" is a single, distinct token so the shared runs stop at the content boundary.
    tok = _fake_tokenizer_by_text(
        prefix=[0], suffix=[2], content_by_text={"a": [10, 11], "0": [12]}
    )
    assert _derive_special_affixes(tok) == ([0], [2])


# --- windowing plan (G1.13 slice 2) ---


def test_plan_windows_single_window_when_it_fits() -> None:
    # A document at or under the window size is one window — the byte-identical path.
    assert _plan_windows(0, window_size=10, overlap=4) == []
    assert _plan_windows(1, window_size=10, overlap=4) == [(0, 1)]
    assert _plan_windows(10, window_size=10, overlap=4) == [(0, 10)]


def test_plan_windows_overlapping_full_coverage() -> None:
    # window_size 10, overlap 2 → stride 8. Every token index must be covered by some window,
    # the last window is anchored full-size to the document end, and consecutive windows
    # overlap by at least `overlap`.
    plans = _plan_windows(20, window_size=10, overlap=2)
    assert plans[0] == (0, 10)
    assert plans[-1][1] == 20  # anchored to the end
    assert all(e - s == 10 for s, e in plans)  # every window is full-size
    covered: set[int] = set()
    for s, e in plans:
        covered.update(range(s, e))
    assert covered == set(range(20))  # no gap
    for (_s0, e0), (s1, _e1) in zip(plans, plans[1:], strict=False):  # pairwise: n vs n-1
        assert s1 < e0  # consecutive windows overlap


def test_plan_windows_rejects_overlap_ge_window() -> None:
    # An overlap that does not advance the stride would never terminate; refuse it.
    with pytest.raises(ValueError, match="overlap"):
        _plan_windows(100, window_size=10, overlap=10)


# --- multi-window pooling / interior-window selection (G1.13 slice 2) ---


def _window(vec_by_token: list[tuple[int, int, list[float]]]):
    """Build a (token_embeddings, offset_mapping) window from (start_char, end_char, vec) tuples."""
    offsets = [(s, e) for s, e, _ in vec_by_token]
    emb = torch.tensor([[v for _, _, v in vec_by_token]])  # (1, n_tokens, hidden)
    return emb, offsets


def test_pool_span_selects_most_interior_window() -> None:
    # Two overlapping windows cover the same char range with *different* marker vectors, so we
    # can see which window a span was pooled from. Window A covers chars [0,40), window B
    # [20,60). A span at [22,26) overlaps both, but is far more interior to B (min-edge-dist
    # 22-20=2 vs 40-26=14 → wait that's A) ... pick a span clearly interior to B.
    win_a = _window([(i * 2, i * 2 + 2, [1.0, 0.0]) for i in range(20)])  # chars 0..40
    win_b = _window([(20 + i * 2, 20 + i * 2 + 2, [0.0, 1.0]) for i in range(20)])  # chars 20..60
    ctx = DocumentContext.from_windows([win_a, win_b], windowing={"overlap": 4})

    # A span at [50,54) only exists in window B → pooled to B's marker [0,1].
    only_b = ctx.pool_span(50, 54)
    assert only_b == pytest.approx([0.0, 1.0])

    # A span at [4,8) only exists in window A → pooled to A's marker [1,0].
    only_a = ctx.pool_span(4, 8)
    assert only_a == pytest.approx([1.0, 0.0])

    # A span at [30,34) is in both windows' overlap. Distance to A's edges: min(30-0, 40-34)=6.
    # Distance to B's edges: min(30-20, 60-34)=10. B is more interior → B's marker wins.
    overlap_span = ctx.pool_span(30, 34)
    assert overlap_span == pytest.approx([0.0, 1.0])


def test_pool_span_every_span_gets_a_nonzero_vector_across_windows() -> None:
    # A document spanning >2 windows: every span that has tokens pools to a non-zero vector
    # (the silent-truncation failure mode is exactly a zero vector for a late span).
    windows = [
        _window([(base + i * 2, base + i * 2 + 2, [1.0, float(w)]) for i in range(10)])
        for w, base in enumerate((0, 16, 32))
    ]
    ctx = DocumentContext.from_windows(windows, windowing={"overlap": 4})
    for start in range(0, 50, 3):
        vec = ctx.pool_span(start, start + 2)
        assert vec is not None, f"span at {start} unexpectedly pooled to no token"
        assert any(c != 0.0 for c in vec), f"span at {start} pooled to a zero vector"


def test_pool_span_no_token_overlap_returns_none() -> None:
    # A whitespace-only span overlaps no token in any window → None, not a zero-vector sentinel
    # (review R3). Callers skip None so no meaningless vector reaches pgvector.
    win = _window([(0, 5, [1.0, 0.0]), (7, 12, [0.0, 1.0])])
    ctx = DocumentContext.from_windows([win], windowing={})
    assert ctx.pool_span(5, 7) is None


def test_window_layout_reports_count_and_boundaries() -> None:
    win_a = _window([(0, 5, [1.0, 0.0])])
    win_b = _window([(3, 9, [0.0, 1.0])])
    ctx = DocumentContext.from_windows(
        [win_a, win_b], windowing={"overlap": 4, "model_max_tokens": 8192}
    )
    layout = ctx.window_layout()
    assert layout["count"] == 2
    assert layout["boundaries"] == [[0, 5], [3, 9]]
    assert layout["overlap"] == 4
    # The policy view excludes the data-dependent count/boundaries.
    assert "count" not in ctx.windowing_policy()
    assert "boundaries" not in ctx.windowing_policy()


def test_document_context_pool_span():
    # offset mappings are usually like [(0,0), (0, 5), (5, 6), (7, 12), (0,0)]
    offset_mapping = [
        (0, 0),  # [CLS]
        (0, 5),  # Hello
        (5, 6),  # ,
        (7, 12),  # world
        (0, 0),  # [SEP]
    ]
    # 5 tokens, embedding dim 2
    embeddings = torch.tensor(
        [
            [
                [0.1, 0.1],
                [1.0, 0.0],  # token 1
                [0.0, 1.0],  # token 2
                [2.0, 2.0],  # token 3
                [0.9, 0.9],
            ]
        ]
    )
    ctx = DocumentContext(token_embeddings=embeddings, offset_mapping=offset_mapping)

    # Test overlap with "Hello," (chars 0 to 6)
    # Should average token 1 and token 2
    # mean([1.0, 0.0], [0.0, 1.0]) = [0.5, 0.5]
    # normalize([0.5, 0.5]) = [0.7071, 0.7071]
    res1 = ctx.pool_span(0, 6)
    assert len(res1) == 2
    assert res1[0] == pytest.approx(0.7071, abs=1e-3)
    assert res1[1] == pytest.approx(0.7071, abs=1e-3)

    # Test overlap with only whitespace (char 6 to 7) → None, not a zero vector (review R3)
    res2 = ctx.pool_span(6, 7)
    assert res2 is None


def test_pool_span_exact_match():
    # Test an exact single token match
    offset_mapping = [(0, 0), (0, 4)]
    embeddings = torch.tensor(
        [
            [
                [0.1, 0.1],
                [3.0, 4.0],  # token 1
            ]
        ]
    )
    ctx = DocumentContext(token_embeddings=embeddings, offset_mapping=offset_mapping)

    # Length of [3.0, 4.0] is 5.0. Normalized = [0.6, 0.8]
    res = ctx.pool_span(0, 4)
    assert res[0] == pytest.approx(0.6, abs=1e-3)
    assert res[1] == pytest.approx(0.8, abs=1e-3)


def test_mean_pool_normalize_batch():
    # Batch of 2 passages, 2 tokens each, dim 2.
    hidden = torch.tensor(
        [
            [[1.0, 0.0], [0.0, 1.0]],  # mean -> [0.5, 0.5] -> normalized [0.7071, 0.7071]
            [[3.0, 4.0], [3.0, 4.0]],  # mean -> [3.0, 4.0] -> normalized [0.6, 0.8]
        ]
    )
    mask = torch.tensor([[1, 1], [1, 1]])

    out = mean_pool_normalize(hidden, mask)

    assert len(out) == 2
    assert out[0][0] == pytest.approx(0.7071, abs=1e-3)
    assert out[0][1] == pytest.approx(0.7071, abs=1e-3)
    assert out[1][0] == pytest.approx(0.6, abs=1e-3)
    assert out[1][1] == pytest.approx(0.8, abs=1e-3)
    # Every row is unit-norm.
    for vec in out:
        assert sum(c * c for c in vec) == pytest.approx(1.0, abs=1e-5)


def test_mean_pool_normalize_ignores_padding():
    # A real token [3.0, 4.0] plus a padded position. The padded token carries a
    # wild value; masking must exclude it so the result equals the unpadded one.
    padded = mean_pool_normalize(
        torch.tensor([[[3.0, 4.0], [999.0, -999.0]]]),
        torch.tensor([[1, 0]]),
    )
    unpadded = mean_pool_normalize(
        torch.tensor([[[3.0, 4.0]]]),
        torch.tensor([[1]]),
    )
    assert padded[0] == pytest.approx(unpadded[0], abs=1e-5)
    assert padded[0][0] == pytest.approx(0.6, abs=1e-3)
    assert padded[0][1] == pytest.approx(0.8, abs=1e-3)


# --- substrate lifecycle (G1.17 R6) ---


def test_substrate_close_releases_and_is_idempotent(monkeypatch) -> None:
    # Mock the model load so no weights are downloaded; close() must drop the references and be
    # safe to call twice (review R6).
    monkeypatch.setattr(emb, "AutoTokenizer", MagicMock())
    monkeypatch.setattr(emb, "AutoModel", MagicMock())
    monkeypatch.setattr(emb.torch.cuda, "is_available", lambda: False)

    substrate = EmbeddingSubstrate(device="cpu")
    assert substrate.model is not None and substrate.tokenizer is not None

    substrate.close()
    assert substrate.model is None and substrate.tokenizer is None
    substrate.close()  # idempotent — no AttributeError on the already-released handles


def test_substrate_context_manager_closes_on_exit(monkeypatch) -> None:
    monkeypatch.setattr(emb, "AutoTokenizer", MagicMock())
    monkeypatch.setattr(emb, "AutoModel", MagicMock())
    monkeypatch.setattr(emb.torch.cuda, "is_available", lambda: False)

    with EmbeddingSubstrate(device="cpu") as substrate:
        assert substrate.model is not None
    assert substrate.model is None and substrate.tokenizer is None
