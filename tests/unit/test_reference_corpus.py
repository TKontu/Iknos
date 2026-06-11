"""Unit tests for the reference-corpus seal helpers (G1.8; §6.1).

DB-free: the pure surface of ``core/reference_corpus.py`` (the tier guard + the content
digest) and the ``reference_box`` constructor. The DB-backed seal read/write and the
``ingest_reference_document`` amortization are covered live in
``tests/integration/test_reference_corpus.py``.
"""

import hashlib

import pytest

from iknos.boxes.serde import box_id_for, reference_box
from iknos.core.reference_corpus import document_input_sha256, validate_sealable_tier
from iknos.types.nodes import BoxStatus, Tier

# --- validate_sealable_tier: only reference/schema may be sealed read-only ---


@pytest.mark.parametrize("tier", [Tier.REFERENCE, Tier.SCHEMA])
def test_sealable_tiers_pass(tier: Tier) -> None:
    validate_sealable_tier(tier)  # does not raise


@pytest.mark.parametrize("tier", [Tier.CASE, Tier.WORKING])
def test_non_reference_tiers_refused(tier: Tier) -> None:
    # case/working are the per-investigation regime — never amortized read-only (§6.1).
    with pytest.raises(ValueError, match="reference/schema-tier"):
        validate_sealable_tier(tier)


# --- document_input_sha256: the immutability key is the content itself ---


def test_input_digest_is_content_sha256() -> None:
    raw = "Bearing 3 vibration exceeded the alarm threshold."
    assert document_input_sha256(raw) == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_input_digest_changes_with_content() -> None:
    assert document_input_sha256("alpha") != document_input_sha256("alpha ")


# --- reference_box: a reference-tier Box with the deterministic-id discipline ---


def test_reference_box_is_reference_tier_and_active() -> None:
    box = reference_box("pump-handbook", "1", "handbook.pdf", 0.9)
    assert box.tier is Tier.REFERENCE
    assert box.status is BoxStatus.ACTIVE
    assert box.id == box_id_for("pump-handbook", "1")


def test_reference_box_id_is_deterministic_per_version() -> None:
    a = reference_box("pump-handbook", "1", "handbook.pdf", 0.9)
    b = reference_box("pump-handbook", "1", "elsewhere.pdf", 0.5)
    c = reference_box("pump-handbook", "2", "handbook.pdf", 0.9)
    assert a.id == b.id  # id keys on (name, version), not content
    assert a.id != c.id  # a new version is a new box
