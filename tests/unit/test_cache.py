"""Unit tests for the content-addressed extraction cache key (G1.7) — pure, no DB/LLM/torch.

The idempotency soundness rests on one property: the hash changes iff something that shapes the
extraction output changes. So every input dimension is pinned to independently move the digest
(determinism + sensitivity), exactly like ``test_ingest.py`` does for ``span_content_hash``.
"""

import pytest

from iknos.core.cache import (
    canonical_json_sha256,
    extraction_content_hash,
    sha256_hex,
)

_BASE = {
    "target_text": "The bearing failed under load.",
    "context_text": "The pump ran for 9000 hours.",
    "context_span_ids": ["11111111-1111-1111-1111-111111111111"],
    "model": "BAAI/extractor-v1",
    "schema_version": 1,
    "prompt_sha": "a" * 64,
    "schema_sha": "b" * 64,
    "sampling": {"temperature": 0.0, "n_samples": 1},
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
    # A deliberate output-shape bump still invalidates (it stays in the key alongside the SHAs).
    assert _hash(schema_version=2) != _hash()


def test_prompt_sha_changes_hash() -> None:
    # G1.15: the rendered prompt is in the key, so a reworded prompt re-extracts even if
    # EXTRACT_SCHEMA_VERSION was not bumped — the silent-staleness hole this closes.
    assert _hash(prompt_sha="c" * 64) != _hash()


def test_schema_sha_changes_hash() -> None:
    # G1.15: a changed guided-decode schema invalidates without a manual version bump.
    assert _hash(schema_sha="d" * 64) != _hash()


def test_sampling_regime_changes_hash() -> None:
    assert _hash(sampling={"temperature": 0.7, "n_samples": 1}) != _hash()


def test_n_samples_changes_hash() -> None:
    # Multi-sample changes both the output and the agreement signal → must re-extract.
    assert _hash(sampling={"temperature": 0.0, "n_samples": 3}) != _hash()


# --- context span identity (G1.24) ---


def test_context_span_ids_change_hash() -> None:
    # G1.24: a re-segmentation that swaps which spans front the window re-keys even when the
    # rendered context_text is held fixed — the span set is keyed explicitly, not inferred.
    assert _hash(context_span_ids=["22222222-2222-2222-2222-222222222222"]) != _hash()


def test_context_span_id_order_changes_hash() -> None:
    # The context window is an ordered sequence, so reordering the same spans is a different key
    # (unlike the order-insensitive sampling dict) — it changes what the model reads first.
    two = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    assert _hash(context_span_ids=two) != _hash(context_span_ids=list(reversed(two)))


def test_verifier_is_not_in_the_key() -> None:
    # G1.22: the extractor's output is independent of the verifier, so the verifier signature is
    # not an extraction input — a verifier toggle/upgrade must NOT invalidate the extraction key
    # (it drives verify-backfill instead). The function rejects a `verifier` kwarg entirely.

    with pytest.raises(TypeError):
        extraction_content_hash(**{**_BASE, "verifier": {"model": "v"}})  # type: ignore[arg-type]


# --- shared hashing helpers (the G1.15 canonicalization primitives) ---


def test_sha256_hex_shape_and_determinism() -> None:
    h = sha256_hex("the rendered prompt")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == sha256_hex("the rendered prompt")
    assert h != sha256_hex("the rendered prompt.")  # one char moves it


def test_canonical_json_sha_is_key_order_insensitive() -> None:
    # The property the G1.15 test calls for: re-ordering schema keys must NOT change the digest.
    assert canonical_json_sha256({"a": 1, "b": [2, 3]}) == canonical_json_sha256(
        {"b": [2, 3], "a": 1}
    )


def test_canonical_json_sha_is_value_sensitive() -> None:
    assert canonical_json_sha256({"a": 1}) != canonical_json_sha256({"a": 2})
