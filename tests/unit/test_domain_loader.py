"""Unit tests for the pure loader result-builder (G0.7, V11; §9, §14).

``domain/loader.py`` is otherwise all-DB — ``load_pack`` MERGEs the Box + taxonomy and
the round-trip (first load, idempotent re-load, content-hash immutability) is owned by
``tests/integration/test_domain_pack_load.py``. The one DB-free seam is ``_loaded_result``:
it builds the :class:`LoadedPack` return value purely from the declaration, which is what
lets a no-op re-load report the **same** ids/counts a fresh load would without re-querying
the graph (``load_pack`` docstring). That fidelity invariant is pinned here, no DB needed.
"""

from iknos.domain.loader import _loaded_result
from iknos.domain.pack import DomainPack


def _pack(**overrides: object) -> DomainPack:
    base: dict[str, object] = {
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
    return DomainPack.from_dict(base)


def _entity_ids(pack: DomainPack) -> dict[str, object]:
    # Mirrors load_pack's own construction so the test exercises the real call shape.
    return {e.key: pack.entity_id(e.key) for e in pack.entities}


def test_reports_pack_box_id_and_entity_ids() -> None:
    pack = _pack()
    ids = _entity_ids(pack)
    result = _loaded_result(pack, ids, already_loaded=False)
    assert result.box_id == pack.box_id
    assert result.entity_ids == ids


def test_counts_are_derived_from_the_declaration() -> None:
    pack = _pack()
    result = _loaded_result(pack, _entity_ids(pack), already_loaded=False)
    # One declared step; component-integral so the closure equals the single direct edge.
    assert result.direct_part_of == len(pack.part_of) == 1
    assert result.part_of == len(pack.transitive_closure()) == 1


def test_part_of_counts_the_rollup_closure_not_just_direct_edges() -> None:
    # A 3-deep component-integral chain (part ⊂ sub ⊂ whole) rolls up: the closure adds the
    # transitive part⊂whole edge, so part_of (closure) must exceed direct_part_of.
    pack = _pack(
        entity_types=[{"name": "Assembly"}, {"name": "Component"}],
        entities=[
            {"key": "whole", "label": "Whole", "type": "Assembly"},
            {"key": "sub", "label": "Sub", "type": "Component"},
            {"key": "part", "label": "Part", "type": "Component"},
        ],
        part_of=[
            {"part": "sub", "whole": "whole", "meronymy": "component-integral"},
            {"part": "part", "whole": "sub", "meronymy": "component-integral"},
        ],
    )
    result = _loaded_result(pack, _entity_ids(pack), already_loaded=False)
    assert result.direct_part_of == 2
    assert result.part_of == 3  # 2 direct + 1 rolled-up part⊂whole
    assert result.part_of == len(pack.transitive_closure())


def test_noop_reload_reports_identical_ids_and_counts_to_a_fresh_load() -> None:
    # The contract that makes the no-op branch honest: an already_loaded=True result is
    # identical to a fresh one except for the flag — so a re-load never lies about the graph.
    pack = _pack()
    ids = _entity_ids(pack)
    fresh = _loaded_result(pack, ids, already_loaded=False)
    reloaded = _loaded_result(pack, ids, already_loaded=True)
    assert fresh.already_loaded is False
    assert reloaded.already_loaded is True
    assert (reloaded.box_id, reloaded.entity_ids, reloaded.direct_part_of, reloaded.part_of) == (
        fresh.box_id,
        fresh.entity_ids,
        fresh.direct_part_of,
        fresh.part_of,
    )


def test_already_loaded_defaults_to_false() -> None:
    # load_pack's first-load path relies on the dataclass default (it omits the kwarg there).
    pack = _pack()
    assert _loaded_result(pack, _entity_ids(pack), already_loaded=False).already_loaded is False
