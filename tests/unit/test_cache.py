"""Unit tests for the content-addressed extraction cache key (G1.7) — pure, no DB/LLM/torch.

The idempotency soundness rests on one property: the hash changes iff something that shapes the
extraction output changes. So every input dimension is pinned to independently move the digest
(determinism + sensitivity), exactly like ``test_ingest.py`` does for ``span_content_hash``.
"""

from iknos.core.cache import extraction_content_hash

_BASE = {
    "target_text": "The bearing failed under load.",
    "context_text": "The pump ran for 9000 hours.",
    "model": "BAAI/extractor-v1",
    "schema_version": 1,
    "sampling": {"temperature": 0.0, "n_samples": 1},
    "verifier": None,
}


def _hash(**overrides: object) -> str:
    return extraction_content_hash(**{**_BASE, **overrides})  # type: ignore[arg-type]


# --- shape & determinism ---


def test_hash_is_sha256_hex() -> None:
    h = _hash()
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_hash_is_deterministic() -> None:
    assert _hash() == _hash()


def test_sampling_key_order_does_not_matter() -> None:
    # Canonicalized (sort_keys) — reordering the regime dict must not change the digest.
    assert _hash(sampling={"n_samples": 1, "temperature": 0.0}) == _hash(
        sampling={"temperature": 0.0, "n_samples": 1}
    )


# --- each input dimension independently moves the digest ---


def test_target_text_changes_hash() -> None:
    assert _hash(target_text="The bearing held.") != _hash()


def test_context_text_changes_hash() -> None:
    # Context feeds reference resolution → part of what the model saw → part of the key.
    assert _hash(context_text="A different preceding paragraph.") != _hash()


def test_model_changes_hash() -> None:
    # The production bug this fixes: an upgraded extractor must not reuse the old extraction.
    assert _hash(model="BAAI/extractor-v2") != _hash()


def test_schema_version_changes_hash() -> None:
    # Bumping EXTRACT_SCHEMA_VERSION (reworded prompt / changed schema) invalidates the cache.
    assert _hash(schema_version=2) != _hash()


def test_sampling_regime_changes_hash() -> None:
    assert _hash(sampling={"temperature": 0.7, "n_samples": 1}) != _hash()


def test_n_samples_changes_hash() -> None:
    # Multi-sample changes both the output and the agreement signal → must re-extract.
    assert _hash(sampling={"temperature": 0.0, "n_samples": 3}) != _hash()


# --- verifier signature ---


def test_enabling_verifier_changes_hash() -> None:
    # Toggling the verifier on changes the derived faithfulness → must invalidate.
    assert _hash(verifier={"model": "verifier-v1", "schema_version": 1}) != _hash()


def test_verifier_model_changes_hash() -> None:
    assert _hash(verifier={"model": "verifier-v2", "schema_version": 1}) != _hash(
        verifier={"model": "verifier-v1", "schema_version": 1}
    )


def test_verifier_schema_version_changes_hash() -> None:
    assert _hash(verifier={"model": "verifier-v1", "schema_version": 2}) != _hash(
        verifier={"model": "verifier-v1", "schema_version": 1}
    )
