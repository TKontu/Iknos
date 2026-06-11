"""Unit tests for the part-whole / abstraction-level subsystem (Phase 2, G2.5).

DB-free: the correctness-critical pure algorithms — the cycle-safe transitive closure, the
derived-level (partonomy depth) read, the endpoint mapping, and the write contracts. The full
induce → closure → level path runs against live AGE in ``tests/integration/test_partwhole.py``.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from iknos.core.extract import NodeKind
from iknos.core.partwhole import (
    INDUCED_CONFIDENCE,
    PARTWHOLE_SCHEMA_VERSION,
    MeronymyInducer,
    _acyclic_edges,
    _PartOfOut,
    derived_level,
    direct_part_of_props,
    part_of_props,
    transitive_closure,
)
from iknos.core.reference import group_referents
from iknos.types.edges import AttachmentProvenance, MeronymyType, is_transitive

# Stable ids for deterministic closure assertions.
ROLLER = uuid.UUID(int=1)
BEARING = uuid.UUID(int=2)
SHAFT = uuid.UUID(int=3)
GEARBOX = uuid.UUID(int=4)
HOUSING = uuid.UUID(int=5)


# --- transitivity rule (§14) ---


def test_only_component_integral_is_transitive():
    assert is_transitive(MeronymyType.COMPONENT_INTEGRAL) is True
    for mt in MeronymyType:
        if mt is not MeronymyType.COMPONENT_INTEGRAL:
            assert is_transitive(mt) is False


# --- cycle-safe transitive closure ---


def test_closure_of_a_chain():
    # roller -> bearing -> shaft -> gearbox
    edges = [(ROLLER, BEARING), (BEARING, SHAFT), (SHAFT, GEARBOX)]
    closure, cyclic = transitive_closure(edges)
    assert cyclic == frozenset()
    assert (ROLLER, GEARBOX) in closure  # transitive ancestor
    assert (ROLLER, SHAFT) in closure
    assert (BEARING, GEARBOX) in closure
    # 3 + 2 + 1 = 6 ancestor pairs
    assert len(closure) == 6


def test_closure_of_a_diamond_dag():
    # roller is part of both bearing and housing; both part of gearbox.
    edges = [(ROLLER, BEARING), (ROLLER, HOUSING), (BEARING, GEARBOX), (HOUSING, GEARBOX)]
    closure, cyclic = transitive_closure(edges)
    assert cyclic == frozenset()
    # roller's ancestor set is {bearing, housing, gearbox} — gearbox counted once.
    assert {a for c, a in closure if c == ROLLER} == {BEARING, HOUSING, GEARBOX}


def test_closure_excludes_and_flags_a_cycle():
    # A meronymy cycle X->Y->X is a contradiction: excluded from roll-up and flagged.
    a, b = uuid.UUID(int=10), uuid.UUID(int=11)
    edges = [(a, b), (b, a), (ROLLER, BEARING)]
    closure, cyclic = transitive_closure(edges)
    assert cyclic == frozenset({a, b})
    # The acyclic edge still closes; the cyclic pair contributes nothing.
    assert (ROLLER, BEARING) in closure
    assert all(a not in pair and b not in pair for pair in closure)


def test_closure_drops_self_loop():
    closure, cyclic = transitive_closure([(ROLLER, ROLLER)])
    assert closure == set()
    # a self-loop is a degenerate cycle but a single self-loop node is not "caught between" —
    # it simply contributes no ancestor; it is excluded from closure.
    assert ROLLER not in {c for c, _ in closure}


def test_closure_is_order_independent():
    forward = [(ROLLER, BEARING), (BEARING, SHAFT)]
    reversed_ = [(BEARING, SHAFT), (ROLLER, BEARING)]
    assert transitive_closure(forward)[0] == transitive_closure(reversed_)[0]


# --- _acyclic_edges directly ---


def test_acyclic_edges_separates_cycle_nodes():
    a, b, c = uuid.UUID(int=20), uuid.UUID(int=21), uuid.UUID(int=22)
    acyclic, cyclic = _acyclic_edges([(a, b), (b, c), (c, a)])
    assert cyclic == frozenset({a, b, c})
    assert acyclic == []


# --- derived level (partonomy depth) ---


def test_derived_level_is_ancestor_count():
    edges = [(ROLLER, BEARING), (BEARING, SHAFT), (SHAFT, GEARBOX)]
    closure, _ = transitive_closure(edges)
    assert derived_level(closure, ROLLER) == 3  # finest
    assert derived_level(closure, BEARING) == 2
    assert derived_level(closure, GEARBOX) == 0  # coarsest — no parent


def test_derived_level_counts_distinct_ancestors_in_dag():
    edges = [(ROLLER, BEARING), (ROLLER, HOUSING), (BEARING, GEARBOX), (HOUSING, GEARBOX)]
    closure, _ = transitive_closure(edges)
    # {bearing, housing, gearbox} — gearbox once despite two paths.
    assert derived_level(closure, ROLLER) == 3


# --- endpoint mapping (pure method) ---


def _inducer() -> MeronymyInducer:
    llm = MagicMock()
    llm.model = "test-model"
    return MeronymyInducer(llm)


def _referents(*labels):
    rows = [(uuid.uuid4(), lab, "", NodeKind.OBJECT) for lab in labels]
    refs = group_referents(rows)
    return {r.norm: r for r in refs}, {r.norm: r for r in refs}


def test_resolve_endpoints_maps_labels_to_canonical_ids():
    by_norm_a, by_norm = _referents("the bearing", "gearbox")
    rels = [_PartOfOut(child="bearing", parent="gearbox")]
    direct = _inducer()._resolve_endpoints(rels, by_norm)
    assert len(direct) == 1
    assert direct[0].child == by_norm["bearing"].canonical
    assert direct[0].parent == by_norm["gearbox"].canonical


def test_resolve_endpoints_drops_unresolved_endpoint():
    _, by_norm = _referents("bearing")  # no "gearbox" entity in the box
    rels = [_PartOfOut(child="bearing", parent="gearbox")]
    assert _inducer()._resolve_endpoints(rels, by_norm) == []


def test_resolve_endpoints_drops_self_loop():
    _, by_norm = _referents("bearing")
    rels = [_PartOfOut(child="the bearing", parent="bearing")]  # same normalized entity
    assert _inducer()._resolve_endpoints(rels, by_norm) == []


# --- detection schema ---


def test_partof_out_defaults_to_component_integral():
    m = _PartOfOut(child="bearing", parent="gearbox")
    assert m.meronymy_type is MeronymyType.COMPONENT_INTEGRAL


# --- write contracts ---


def test_direct_part_of_props():
    box = uuid.uuid4()
    now = datetime(2026, 6, 11, tzinfo=UTC)
    props = direct_part_of_props(
        box=box,
        meronymy_type=MeronymyType.MEMBER_COLLECTION,
        provenance=AttachmentProvenance.INDUCED,
        confidence=INDUCED_CONFIDENCE,
        now=now,
    )
    assert props["box"] == str(box)
    assert props["meronymy_type"] == "member-collection"
    assert props["provenance"] == "induced"
    assert props["support_count"] == 1
    assert props["confidence"] == INDUCED_CONFIDENCE
    assert props["valid_to"] is None
    assert props["event_time"] is None
    assert props["valid_from"] == "2026-06-11T00:00:00+00:00"


def test_part_of_props_is_always_component_integral():
    props = part_of_props(
        box=uuid.uuid4(),
        provenance=AttachmentProvenance.INDUCED,
        confidence=0.5,
        now=datetime(2026, 6, 11, tzinfo=UTC),
    )
    # The closure edge always carries the only transitivity-safe subtype (§14).
    assert props["meronymy_type"] == "component-integral"


def test_schema_version_is_recorded_constant():
    assert PARTWHOLE_SCHEMA_VERSION == 1
