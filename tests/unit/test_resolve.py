"""Unit tests for the entity-resolution cascade (Phase 2, G2.3).

DB-free: the pure cascade — normalization, blocking, scoring, the decision bars, the
``SAME_AS`` write contract, and component union-find. The full load → persist path runs
against live AGE in ``tests/integration/test_resolve.py`` (CI).
"""

import uuid
from datetime import UTC, datetime

import pytest

from iknos.core.extract import NodeKind
from iknos.core.resolve import (
    RESOLVE_CANDIDATE_BAR,
    RESOLVE_CONFIRM_BAR,
    RESOLVE_SCHEMA_VERSION,
    EntityRecord,
    block_candidates,
    canonical_id,
    components,
    decide,
    normalize_label,
    same_as_to_props,
    score_pair,
)
from iknos.types.edges import SameAsState

# --- normalization ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The bearing", "bearing"),
        ("a Pump", "pump"),
        ("an Operator", "operator"),
        ("  Bearing   3  ", "bearing 3"),
        ("high-speed shaft", "high speed shaft"),
        ("the The", "the"),  # only one leading article stripped
        ("PUMP.", "pump"),
    ],
)
def test_normalize_label(raw, expected):
    assert normalize_label(raw) == expected


# --- blocking ---


def _ent(label, *, kind=NodeKind.OBJECT, type="", roles=(), context=()):
    return EntityRecord(
        id=uuid.uuid4(),
        label=label,
        type=type,
        kind=kind,
        box=uuid.uuid4(),
        roles=frozenset(roles),
        context=frozenset(context),
    )


def test_block_pairs_share_a_token_same_kind():
    a = _ent("the bearing")
    b = _ent("bearing housing")  # shares token "bearing"
    c = _ent("pump")  # shares nothing
    pairs = block_candidates([a, b, c])
    ids = {frozenset((x.id, y.id)) for x, y in pairs}
    assert ids == {frozenset((a.id, b.id))}


def test_block_does_not_pair_across_kinds():
    actor = _ent("bearing", kind=NodeKind.ACTOR)
    obj = _ent("bearing", kind=NodeKind.OBJECT)
    assert block_candidates([actor, obj]) == []


def test_block_dedupes_pairs_sharing_multiple_tokens():
    a = _ent("high speed shaft")
    b = _ent("high speed bearing")  # shares "high" and "speed" — still one pair
    pairs = block_candidates([a, b])
    assert len(pairs) == 1


# --- scoring (relational/contextual, similarity is blocking-only) ---


def test_score_confirms_on_exact_label_type_and_relational_context():
    # Same label + type + a shared neighbour + shared role -> over the confirm bar.
    a = _ent("operator", kind=NodeKind.ACTOR, type="person", roles=["subject"], context=["pump"])
    b = _ent("operator", kind=NodeKind.ACTOR, type="person", roles=["subject"], context=["pump"])
    s = score_pair(a, b)
    assert s >= RESOLVE_CONFIRM_BAR


def test_score_label_plus_type_alone_is_candidate_not_confirm():
    # The conservative under-merge default: exact label + type but NO relational context
    # lands in the candidate band, never an auto-merge.
    a = _ent("bearing", type="component")
    b = _ent("bearing", type="component")
    s = score_pair(a, b)
    assert RESOLVE_CANDIDATE_BAR <= s < RESOLVE_CONFIRM_BAR


def test_score_conflicting_type_suppresses_below_candidate():
    # A conflicting non-empty type is disconfirming even with an exact label.
    a = _ent("valve", type="plumbing")
    b = _ent("valve", type="anatomy")
    assert score_pair(a, b) < RESOLVE_CANDIDATE_BAR


def test_score_distinct_labels_stay_below_candidate_bar():
    # Blocked on a shared token ("bearing") but distinct normalized labels: shared type is
    # the only (weak) signal, so the pair stays below the candidate bar -> no edge.
    a = _ent("bearing 3", type="component")
    b = _ent("bearing 4", type="component")
    assert score_pair(a, b) < RESOLVE_CANDIDATE_BAR
    assert decide(score_pair(a, b)) is None


def test_score_relational_context_monotonic():
    base = _ent("operator", type="person", context=[])
    one = _ent("operator", type="person", context=["pump"])
    two = _ent("operator", type="person", context=["pump", "valve"])
    ref = _ent("operator", type="person", context=["pump", "valve"])
    assert score_pair(base, ref) < score_pair(one, ref) < score_pair(two, ref)


# --- decision bars ---


def test_decide_thresholds():
    assert decide(RESOLVE_CONFIRM_BAR) is SameAsState.CONFIRMED
    assert decide(RESOLVE_CONFIRM_BAR - 1e-9) is SameAsState.CANDIDATE
    assert decide(RESOLVE_CANDIDATE_BAR) is SameAsState.CANDIDATE
    assert decide(RESOLVE_CANDIDATE_BAR - 1e-9) is None
    assert decide(0.0) is None


# --- SAME_AS write contract ---


def test_same_as_to_props_flattens_state_strength_annotations_bitemporal():
    box = uuid.uuid4()
    now = datetime(2026, 6, 11, tzinfo=UTC)
    props = same_as_to_props(box=box, state=SameAsState.CONFIRMED, strength=0.92, now=now)
    assert props["box"] == str(box)
    assert props["state"] == "confirmed"
    assert props["strength"] == 0.92
    # The two §12 annotations, seeded and uncollapsed.
    assert props["support_count"] == 1
    assert props["confidence"] == 0.92
    # Open bitemporal interval; no event time.
    assert props["valid_to"] is None
    assert props["event_time"] is None
    assert props["ingested_at"] == "2026-06-11T00:00:00+00:00"
    assert props["valid_from"] == "2026-06-11T00:00:00+00:00"


# --- component union-find (confirmed edges only) ---


def test_components_collapse_transitive_chain():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    comps = components([(a, b), (b, c)])  # a-b-c chain; d unrelated
    assert len(comps) == 1
    assert comps[0] == frozenset((a, b, c))
    assert d not in comps[0]


def test_components_omit_singletons():
    a, b, c = (uuid.uuid4() for _ in range(3))
    comps = components([(a, b)])  # c never appears -> no singleton component
    assert comps == [frozenset((a, b))]


def test_canonical_id_is_lexicographically_min():
    ids = [uuid.UUID(int=3), uuid.UUID(int=1), uuid.UUID(int=2)]
    assert canonical_id(frozenset(ids)) == min(ids, key=str)


def test_schema_version_is_recorded_constant():
    assert RESOLVE_SCHEMA_VERSION == 1
