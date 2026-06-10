import torch
from transformers import AutoModel, AutoTokenizer


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


class DocumentContext:
    def __init__(self, token_embeddings: torch.Tensor, offset_mapping: list[tuple[int, int]]):
        self.token_embeddings = token_embeddings  # shape: (1, seq_len, hidden_size)
        self.offset_mapping = offset_mapping  # length: seq_len

    def pool_span(self, start_char: int, end_char: int) -> list[float]:
        """
        Pool the token embeddings that overlap with the character span [start_char, end_char).
        """
        token_indices = []
        for i, (tok_start, tok_end) in enumerate(self.offset_mapping):
            if tok_start == tok_end == 0:
                # Typically special tokens like [CLS] or [SEP]
                continue
            # Overlap condition
            if tok_start < end_char and tok_end > start_char:
                token_indices.append(i)

        if not token_indices:
            # Fallback if no tokens match (e.g., whitespace-only span)
            return [0.0] * self.token_embeddings.shape[-1]

        span_embeddings = self.token_embeddings[0, token_indices, :]

        # Mean pooling
        pooled = span_embeddings.mean(dim=0)

        # Normalize (bge-m3 uses cosine similarity)
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
        """
        Embed the document and return the context holding token embeddings.
        Currently handles up to max model length.
        """
        inputs = self.tokenizer(
            text, return_tensors="pt", return_offsets_mapping=True, truncation=True, max_length=8192
        )

        offset_mapping = inputs.pop("offset_mapping")[0].tolist()
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            token_embeddings = outputs.last_hidden_state

        return DocumentContext(
            token_embeddings=token_embeddings.cpu(), offset_mapping=offset_mapping
        )

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
            max_length=8192,
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            token_embeddings = outputs.last_hidden_state

        return mean_pool_normalize(token_embeddings.cpu(), inputs["attention_mask"].cpu())
