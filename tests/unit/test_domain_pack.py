"""Unit tests for domain-pack declaration + part-whole closure (G0.7).

Pure (no database): structural validation fails fast, the §14 component-integral
roll-up rule is exact, and ids are deterministic. Persistence is covered by
``tests/integration/test_domain_pack_load.py``.
"""

import pytest

from iknos.domain.pack import DomainPack, MeronymyType
from iknos.domain.packs import bundled_pack
from iknos.types.nodes import Tier


def _pack(**overrides) -> dict:
    base = {
        "name": "t",
        "version": "1.0.0",
        "tier": "reference",
        "source": "test",
        "reliability_prior": 0.9,
        "entity_types": [{"name": "Assembly"}, {"name": "Component"}],
        "entities": [
            {"key": "whole", "label": "Whole", "type": "Assembly"},
            {"key": "part", "label": "Part", "type": "Component"},
        ],
        "part_of": [{"part": "part", "whole": "whole", "meronymy": "component-integral"}],
    }
    base.update(overrides)
    return base


# --- fail-fast validation (DB never touched) ---


def test_rejects_case_tier() -> None:
    with pytest.raises(ValueError, match="tier must be"):
        DomainPack.from_dict(_pack(tier="case"))


def test_rejects_unknown_entity_type() -> None:
    with pytest.raises(ValueError, match="not in the entity-type ontology"):
        DomainPack.from_dict(
            _pack(entities=[{"key": "x", "label": "X", "type": "Nope"}], part_of=[])
        )


def test_rejects_dangling_part_of_reference() -> None:
    with pytest.raises(ValueError, match="unknown part key"):
        DomainPack.from_dict(
            _pack(part_of=[{"part": "ghost", "whole": "whole", "meronymy": "component-integral"}])
        )


def test_rejects_duplicate_entity_keys() -> None:
    with pytest.raises(ValueError, match="duplicate taxonomy entity keys"):
        DomainPack.from_dict(
            _pack(
                entities=[
                    {"key": "dup", "label": "A", "type": "Assembly"},
                    {"key": "dup", "label": "B", "type": "Component"},
                ],
                part_of=[],
            )
        )


def test_rejects_unknown_entity_type_parent() -> None:
    with pytest.raises(ValueError, match="parent"):
        DomainPack.from_dict(
            _pack(entity_types=[{"name": "Component", "parent": "Ghost"}], part_of=[])
        )


def test_rejects_cycle() -> None:
    with pytest.raises(ValueError, match="cyclic"):
        DomainPack.from_dict(
            _pack(
                entities=[
                    {"key": "a", "label": "A", "type": "Component"},
                    {"key": "b", "label": "B", "type": "Component"},
                ],
                part_of=[
                    {"part": "a", "whole": "b", "meronymy": "component-integral"},
                    {"part": "b", "whole": "a", "meronymy": "component-integral"},
                ],
            )
        )


# --- transitive closure (§14) ---


def test_component_integral_chain_rolls_up() -> None:
    pack = bundled_pack("pump_basic")
    closure = {(e.part, e.whole): e for e in pack.transitive_closure()}

    # All four declared steps are present as 1-hop partOf.
    assert closure[("roller", "bearing")].derivation == "direct"
    assert closure[("bearing", "pump")].derivation == "direct"
    assert closure[("housing", "pump")].derivation == "direct"
    assert closure[("steel", "bearing")].derivation == "direct"

    # roller -> pump rolls up through two component-integral hops.
    assert ("roller", "pump") in closure
    assert closure[("roller", "pump")].derivation == "rollup"
    assert closure[("roller", "pump")].meronymy is MeronymyType.COMPONENT_INTEGRAL


def test_non_component_integral_does_not_roll_up() -> None:
    # steel -> bearing is stuff-object; bearing -> pump is component-integral.
    # The chain must NOT yield steel -> pump (transitivity unsafe, §14).
    pack = bundled_pack("pump_basic")
    pairs = {(e.part, e.whole) for e in pack.transitive_closure()}
    assert ("steel", "pump") not in pairs


def test_closure_is_deterministic_and_idempotent() -> None:
    pack = bundled_pack("pump_basic")
    once = pack.transitive_closure()
    twice = pack.transitive_closure()
    assert once == twice  # pure, order-stable


def test_mixed_chain_blocks_rollup_midway() -> None:
    # a -ci-> b -stuff-> c -ci-> d : only a->b and the c->d step are safe; no
    # rollup crosses the stuff-object edge, so a->d and a->c never appear.
    pack = DomainPack.from_dict(
        _pack(
            entity_types=[{"name": "Component"}, {"name": "Material"}],
            entities=[
                {"key": "a", "label": "A", "type": "Component"},
                {"key": "b", "label": "B", "type": "Component"},
                {"key": "c", "label": "C", "type": "Material"},
                {"key": "d", "label": "D", "type": "Component"},
            ],
            part_of=[
                {"part": "a", "whole": "b", "meronymy": "component-integral"},
                {"part": "b", "whole": "c", "meronymy": "stuff-object"},
                {"part": "c", "whole": "d", "meronymy": "component-integral"},
            ],
        )
    )
    pairs = {(e.part, e.whole) for e in pack.transitive_closure()}
    assert ("a", "c") not in pairs
    assert ("a", "d") not in pairs
    assert ("b", "d") not in pairs  # b->c is stuff-object, blocks the c->d hop too


# --- deterministic ids ---


def test_ids_are_deterministic_across_instances() -> None:
    p1 = bundled_pack("pump_basic")
    p2 = bundled_pack("pump_basic")
    assert p1.box_id == p2.box_id
    assert p1.entity_id("roller") == p2.entity_id("roller")


def test_version_bump_changes_box_id() -> None:
    base = bundled_pack("pump_basic")
    bumped = base.model_copy(update={"version": "0.2.0"})
    assert bumped.box_id != base.box_id
    # entity ids are namespaced under the box, so they move with the version too
    assert bumped.entity_id("roller") != base.entity_id("roller")


def test_bundled_pack_is_reference_tier() -> None:
    assert bundled_pack("pump_basic").tier is Tier.REFERENCE


# --- content hash / per-version immutability (G0.R1) ---


def test_content_hash_is_stable_across_instances() -> None:
    assert bundled_pack("pump_basic").content_hash == bundled_pack("pump_basic").content_hash


def test_content_hash_is_order_independent() -> None:
    # Reordering entities / part_of / entity_types is not a semantic change, so
    # reindenting or reshuffling the JSON must NOT trip the immutability guard.
    base = _pack(
        entity_types=[{"name": "Assembly"}, {"name": "Component"}],
        entities=[
            {"key": "whole", "label": "Whole", "type": "Assembly"},
            {"key": "part", "label": "Part", "type": "Component"},
            {"key": "part2", "label": "Part 2", "type": "Component"},
        ],
        part_of=[
            {"part": "part", "whole": "whole", "meronymy": "component-integral"},
            {"part": "part2", "whole": "whole", "meronymy": "component-integral"},
        ],
    )
    shuffled = _pack(
        entity_types=[{"name": "Component"}, {"name": "Assembly"}],
        entities=[
            {"key": "part2", "label": "Part 2", "type": "Component"},
            {"key": "part", "label": "Part", "type": "Component"},
            {"key": "whole", "label": "Whole", "type": "Assembly"},
        ],
        part_of=[
            {"part": "part2", "whole": "whole", "meronymy": "component-integral"},
            {"part": "part", "whole": "whole", "meronymy": "component-integral"},
        ],
    )
    assert DomainPack.from_dict(base).content_hash == DomainPack.from_dict(shuffled).content_hash


def test_content_hash_changes_on_semantic_change() -> None:
    base = DomainPack.from_dict(_pack())
    # A different label is a genuine content change → different hash.
    relabelled = DomainPack.from_dict(
        _pack(
            entities=[
                {"key": "whole", "label": "Whole", "type": "Assembly"},
                {"key": "part", "label": "RELABELLED", "type": "Component"},
            ]
        )
    )
    assert base.content_hash != relabelled.content_hash


def test_content_hash_independent_of_version() -> None:
    # The hash guards *content*; version is the identity axis (a bump = a new Box),
    # so two versions of the same content share a content hash by design.
    base = bundled_pack("pump_basic")
    bumped = base.model_copy(update={"version": "0.2.0"})
    assert base.content_hash == bumped.content_hash
    assert base.box_id != bumped.box_id
