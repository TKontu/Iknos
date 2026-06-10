"""Unit tests for the box serialization contract + constructors (G2.1; §9, §10).

DB-free: exercises the pure ``serde`` layer — the Box↔AGE property mapping both the
registry and the pack loader write through. The round-trip is the contract the indexes
(G1.11) and the extract operator (G2.2) depend on, so it is pinned in both directions,
including the agtype read shape (list properties come back as JSON strings).
"""

from datetime import UTC, datetime

from iknos.boxes.serde import (
    box_from_props,
    box_id_for,
    box_to_props,
    case_box,
    resolve_tier,
)
from iknos.types.governance import SourceInterest
from iknos.types.nodes import Box, BoxStatus, Tier

_WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _box(**overrides: object) -> Box:
    base: dict[str, object] = dict(
        id=box_id_for("acme", "1"),
        name="acme",
        tier=Tier.CASE,
        version="1",
        source="acme.pdf",
        reliability_prior=0.7,
        valid_from=_WHEN,
        status=BoxStatus.ACTIVE,
    )
    base.update(overrides)
    return Box(**base)  # type: ignore[arg-type]


# --- box_to_props: the write contract ---


def test_box_to_props_serializes_core_fields() -> None:
    props = box_to_props(_box())
    assert props["tier"] == "case"  # enum -> value, not "Tier.CASE"
    assert props["status"] == "active"
    assert props["valid_from"] == "2026-01-01T12:00:00+00:00"
    assert props["valid_to"] is None
    assert props["reliability_prior"] == 0.7
    assert props["name"] == "acme"


def test_box_to_props_omits_interest_when_none() -> None:
    # Unknown interest (None) emits neither key — preserving the None vs known-empty
    # distinction SourceInterest is built to carry (§9.1).
    props = box_to_props(_box(interest=None))
    assert "interest_role" not in props
    assert "interest_stake" not in props


def test_box_to_props_flattens_interest_when_present() -> None:
    props = box_to_props(_box(interest=SourceInterest(role="supplier", stake={"b", "a"})))
    assert props["interest_role"] == "supplier"
    assert props["interest_stake"] == ["a", "b"]  # sorted


def test_box_to_props_merges_extra_last() -> None:
    props = box_to_props(_box(), extra={"kind": "domain_pack", "content_hash": "abc"})
    assert props["kind"] == "domain_pack"
    assert props["content_hash"] == "abc"


# --- round-trip: write contract ∘ read contract = identity ---


def test_round_trip_no_interest() -> None:
    box = _box(interest=None)
    assert box_from_props(box_to_props(box)) == box


def test_round_trip_with_interest() -> None:
    box = _box(interest=SourceInterest(role="supplier", stake={"a", "b"}))
    assert box_from_props(box_to_props(box)) == box


def test_round_trip_with_valid_to() -> None:
    box = _box(valid_to=datetime(2026, 6, 1, tzinfo=UTC), status=BoxStatus.DEPRECATED)
    assert box_from_props(box_to_props(box)) == box


def test_round_trip_known_empty_interest_distinct_from_none() -> None:
    # SourceInterest() (known, empty) must NOT collapse to None on read.
    box = _box(interest=SourceInterest())
    restored = box_from_props(box_to_props(box))
    assert restored.interest == SourceInterest()
    assert restored.interest is not None


def test_box_from_props_accepts_json_string_stake() -> None:
    # As read back from AGE: cypher_map JSON-encodes a list into a string property.
    props = box_to_props(_box(interest=SourceInterest(role="r", stake={"x", "y"})))
    props["interest_stake"] = '["x", "y"]'  # the agtype-read shape
    restored = box_from_props(props)
    assert restored.interest == SourceInterest(role="r", stake=frozenset({"x", "y"}))


def test_box_from_props_ignores_pack_extras() -> None:
    # A pack box carries kind/content_hash/entity_types; reading it yields a plain Box.
    props = box_to_props(
        _box(tier=Tier.REFERENCE),
        extra={"kind": "domain_pack", "content_hash": "h", "entity_types": [{"name": "X"}]},
    )
    box = box_from_props(props)
    assert box.tier == Tier.REFERENCE
    assert box.name == "acme"


# --- constructors + helpers ---


def test_case_box_is_case_tier_and_deterministic() -> None:
    a = case_box("acme", "1", "acme.pdf", 0.7)
    b = case_box("acme", "1", "other.pdf", 0.9)  # same (name, version) -> same id
    assert a.tier == Tier.CASE
    assert a.status == BoxStatus.ACTIVE
    assert a.id == b.id == box_id_for("acme", "1")
    assert a.valid_from.tzinfo is not None  # stamped, tz-aware


def test_case_box_distinct_versions_distinct_ids() -> None:
    assert case_box("acme", "1", "s", 0.5).id != case_box("acme", "2", "s", 0.5).id


def test_resolve_tier_prefers_override() -> None:
    box = _box(tier=Tier.CASE)
    assert resolve_tier(box) == Tier.CASE
    assert resolve_tier(box, Tier.WORKING) == Tier.WORKING


# --- SourceInterest.flatten() ---


def test_source_interest_flatten_shape() -> None:
    assert SourceInterest(role="supplier", stake={"b", "a"}).flatten() == {
        "interest_role": "supplier",
        "interest_stake": ["a", "b"],
    }
    assert SourceInterest().flatten() == {"interest_role": None, "interest_stake": []}
