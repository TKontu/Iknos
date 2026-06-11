"""Unit tests for the reference-binding cascade (Phase 2, G2.4).

DB-free: the pure cascade — referent grouping, blocking, the deterministic binding score,
the decision bars (confirm / candidate / unresolved + tie-handling), and the Mention /
REFERS_TO write contracts. The full detect → bind → persist path runs against live AGE in
``tests/integration/test_reference.py`` (CI).
"""

import uuid
from datetime import UTC, datetime

from iknos.core.extract import NodeKind
from iknos.core.reference import (
    REFER_CANDIDATE_BAR,
    REFER_CONFIRM_BAR,
    REFER_SCHEMA_VERSION,
    Mention,
    MentionType,
    Referent,
    block_referents,
    decide_binding,
    group_referents,
    mention_to_props,
    refers_to_to_props,
    score_binding,
)
from iknos.types.edges import BindingState

# --- referent grouping (collapse fresh nodes by kind + normalized label) ---


def test_group_collapses_same_label_same_kind():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    refs = group_referents(
        [
            (a, "the bearing", "component", NodeKind.OBJECT),
            (b, "Bearing", "component", NodeKind.OBJECT),  # same norm "bearing"
            (c, "pump", "equipment", NodeKind.OBJECT),
        ]
    )
    by_norm = {r.norm: r for r in refs}
    assert set(by_norm) == {"bearing", "pump"}
    assert by_norm["bearing"].ids == frozenset((a, b))
    assert by_norm["bearing"].canonical == min((a, b), key=str)


def test_group_keeps_kinds_separate():
    a, b = uuid.uuid4(), uuid.uuid4()
    refs = group_referents(
        [
            (a, "bearing", "", NodeKind.ACTOR),
            (b, "bearing", "", NodeKind.OBJECT),
        ]
    )
    assert len(refs) == 2
    assert {r.kind for r in refs} == {NodeKind.ACTOR, NodeKind.OBJECT}


def test_group_drops_empty_normalized_label():
    a = uuid.uuid4()
    assert group_referents([(a, "  .  ", "", NodeKind.OBJECT)]) == []


def test_group_excludes_own_proposition_entities():
    # A mention's own clause entity must not be a binding target (no self-binding) — excluding
    # the only "bearing" id leaves the fuller named entity as the sole referent.
    own = uuid.uuid4()
    other = uuid.uuid4()
    refs = group_referents(
        [
            (own, "the bearing", "component", NodeKind.OBJECT),
            (other, "bearing 3", "component", NodeKind.OBJECT),
        ],
        exclude_ids=frozenset((own,)),
    )
    assert [r.norm for r in refs] == ["bearing 3"]


def test_group_drops_group_emptied_by_exclusion():
    a, b = uuid.uuid4(), uuid.uuid4()
    refs = group_referents(
        [(a, "bearing", "", NodeKind.OBJECT), (b, "bearing", "", NodeKind.OBJECT)],
        exclude_ids=frozenset((a, b)),
    )
    assert refs == []


# --- blocking ---


def _ref(label, *, kind=NodeKind.OBJECT, type=""):
    eid = uuid.uuid4()
    return Referent(canonical=eid, ids=frozenset((eid,)), label=label, type=type, kind=kind)


def _mention(surface, *, mtype=MentionType.DEFINITE, kind=None):
    return Mention(id=uuid.uuid4(), surface=surface, mention_type=mtype, kind=kind)


def test_block_requires_shared_token():
    m = _mention("the bearing")
    bearing = _ref("bearing 3")
    pump = _ref("pump")
    assert block_referents(m, [bearing, pump]) == [bearing]


def test_block_pronoun_has_no_lexical_token_so_blocks_to_empty():
    # "it" normalizes to "it"; no referent shares that token -> un-bindable here (seam).
    m = _mention("it", mtype=MentionType.PRONOUN)
    assert block_referents(m, [_ref("bearing 3"), _ref("pump")]) == []


def test_block_kind_guess_narrows_candidates():
    m = _mention("bearing", kind=NodeKind.OBJECT)
    actor_bearing = _ref("bearing", kind=NodeKind.ACTOR)
    object_bearing = _ref("bearing", kind=NodeKind.OBJECT)
    assert block_referents(m, [actor_bearing, object_bearing]) == [object_bearing]


def test_block_absent_kind_guess_admits_both():
    m = _mention("bearing", kind=None)
    refs = [_ref("bearing", kind=NodeKind.ACTOR), _ref("bearing", kind=NodeKind.OBJECT)]
    assert set(block_referents(m, refs)) == set(refs)


# --- scoring (deterministic lexical + attribute; never similarity/attention) ---


def test_score_exact_label_and_kind_reaches_confirm_bar():
    m = _mention("bearing 3", mtype=MentionType.PROPER, kind=NodeKind.OBJECT)
    r = _ref("bearing 3", kind=NodeKind.OBJECT)
    assert score_binding(m, r) >= REFER_CONFIRM_BAR


def test_score_containment_in_fuller_name_is_candidate_not_confirm():
    # A definite description fully contained in a fuller named entity ("the bearing" subset of
    # "bearing 3") is partial evidence -> candidate band, never an auto-confirm.
    m = _mention("the bearing", kind=NodeKind.OBJECT)
    r = _ref("bearing 3", kind=NodeKind.OBJECT)
    s = score_binding(m, r)
    assert REFER_CANDIDATE_BAR <= s < REFER_CONFIRM_BAR


def test_score_no_overlap_is_zero():
    assert score_binding(_mention("the device"), _ref("bearing 3")) == 0.0


def test_score_pronoun_is_zero():
    # No lexical content -> no in-graph-entity signal (the discourse-antecedent seam).
    assert score_binding(_mention("it", mtype=MentionType.PRONOUN), _ref("bearing 3")) == 0.0


def test_score_partial_token_overlap_monotonic():
    # More of the mention covered by the referent label -> higher containment -> higher score.
    m = _mention("high speed bearing")
    one = _ref("bearing housing")  # covers 1/3
    two = _ref("speed bearing")  # covers 2/3
    assert score_binding(m, one) < score_binding(m, two)


# --- decision bars + tie handling ---


def test_decide_confirms_single_exact_match():
    m = _mention("bearing 3", mtype=MentionType.PROPER, kind=NodeKind.OBJECT)
    r = _ref("bearing 3", kind=NodeKind.OBJECT)
    decision = decide_binding(m, [r, _ref("pump")])
    assert decision.state is BindingState.CONFIRMED
    assert decision.resolved is True
    assert [t.canonical for t, _ in decision.targets] == [r.canonical]


def test_decide_unresolved_when_nothing_clears_candidate_bar():
    m = _mention("the device")  # no lexical overlap with any referent
    decision = decide_binding(m, [_ref("bearing 3"), _ref("pump")])
    assert decision.state is None
    assert decision.resolved is False
    assert decision.targets == []


def test_decide_keeps_tied_referents_as_candidates():
    # Two equally-good named bearings -> ambiguous: both stay as CANDIDATE, never confirmed.
    m = _mention("the bearing", kind=NodeKind.OBJECT)
    b3 = _ref("bearing 3", kind=NodeKind.OBJECT)
    b4 = _ref("bearing 4", kind=NodeKind.OBJECT)
    decision = decide_binding(m, [b3, b4])
    assert decision.state is BindingState.CANDIDATE
    assert decision.resolved is False
    assert {t.canonical for t, _ in decision.targets} == {b3.canonical, b4.canonical}


def test_decide_single_partial_match_is_open_candidate():
    m = _mention("the bearing", kind=NodeKind.OBJECT)
    decision = decide_binding(m, [_ref("bearing 3", kind=NodeKind.OBJECT)])
    assert decision.state is BindingState.CANDIDATE
    assert decision.resolved is False
    assert len(decision.targets) == 1


def test_decide_targets_sorted_deterministically():
    m = _mention("the bearing", kind=NodeKind.OBJECT)
    refs = [_ref("bearing 4", kind=NodeKind.OBJECT), _ref("bearing 3", kind=NodeKind.OBJECT)]
    targets = decide_binding(m, refs).targets
    canon = [t.canonical for t, _ in targets]
    # Equal scores -> tie-broken by canonical id ascending; stable across input order.
    assert canon == sorted(canon, key=str)


# --- Mention write contract ---


def test_mention_to_props():
    box = uuid.uuid4()
    m = _mention("the bearing", mtype=MentionType.DEFINITE)
    props = mention_to_props(m, box)
    assert props["id"] == str(m.id)
    assert props["box"] == str(box)
    assert props["surface"] == "the bearing"
    assert props["mention_type"] == "definite"


# --- REFERS_TO write contract ---


def test_refers_to_to_props_flattens_state_strength_annotations_bitemporal():
    box = uuid.uuid4()
    now = datetime(2026, 6, 11, tzinfo=UTC)
    props = refers_to_to_props(box=box, state=BindingState.CANDIDATE, strength=0.6, now=now)
    assert props["box"] == str(box)
    assert props["state"] == "candidate"
    assert props["strength"] == 0.6
    # The two §12 annotations, seeded and uncollapsed.
    assert props["support_count"] == 1
    assert props["confidence"] == 0.6
    # Open bitemporal interval; no event time.
    assert props["valid_to"] is None
    assert props["event_time"] is None
    assert props["ingested_at"] == "2026-06-11T00:00:00+00:00"
    assert props["valid_from"] == "2026-06-11T00:00:00+00:00"


def test_schema_version_is_recorded_constant():
    assert REFER_SCHEMA_VERSION == 1
