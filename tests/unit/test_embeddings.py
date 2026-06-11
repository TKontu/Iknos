import pytest
import torch

from iknos.core.embeddings import (
    MAX_MODEL_TOKENS,
    DocumentContext,
    DocumentTooLongError,
    _raise_if_truncated,
    mean_pool_normalize,
)

# --- truncation guard (G1.13 slice 1) ---


def test_raise_if_truncated_over_limit_refuses() -> None:
    # A token count past the context window would be silently truncated → spans past the
    # cutoff get zero vectors → invisible to dense retrieval. Refuse it loudly instead.
    with pytest.raises(DocumentTooLongError, match="context window"):
        _raise_if_truncated(MAX_MODEL_TOKENS + 1)


def test_raise_if_truncated_at_and_under_limit_pass() -> None:
    # Exactly filling the window (special tokens included) is fine; under it is fine.
    _raise_if_truncated(MAX_MODEL_TOKENS)
    _raise_if_truncated(MAX_MODEL_TOKENS - 100)
    _raise_if_truncated(0)


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

    # Test overlap with only whitespace (char 6 to 7)
    res2 = ctx.pool_span(6, 7)
    assert res2 == [0.0, 0.0]


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
