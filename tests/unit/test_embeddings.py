import torch
import pytest
from iknos.core.embeddings import DocumentContext

def test_document_context_pool_span():
    # offset mappings are usually like [(0,0), (0, 5), (5, 6), (7, 12), (0,0)]
    offset_mapping = [
        (0, 0),    # [CLS]
        (0, 5),    # Hello
        (5, 6),    # ,
        (7, 12),   # world
        (0, 0),    # [SEP]
    ]
    # 5 tokens, embedding dim 2
    embeddings = torch.tensor([[
        [0.1, 0.1],
        [1.0, 0.0],  # token 1
        [0.0, 1.0],  # token 2
        [2.0, 2.0],  # token 3
        [0.9, 0.9]
    ]])
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
    offset_mapping = [(0,0), (0, 4)]
    embeddings = torch.tensor([[
        [0.1, 0.1],
        [3.0, 4.0],  # token 1
    ]])
    ctx = DocumentContext(token_embeddings=embeddings, offset_mapping=offset_mapping)
    
    # Length of [3.0, 4.0] is 5.0. Normalized = [0.6, 0.8]
    res = ctx.pool_span(0, 4)
    assert res[0] == pytest.approx(0.6, abs=1e-3)
    assert res[1] == pytest.approx(0.8, abs=1e-3)
