"""Unit tests for the entity-linking / taxonomy-anchoring cascade (Phase 2, G2.8).

DB-free: the pure cascade — blocking, scoring, the decision bars, the ``ANCHORS_TO`` write
contract, and the coverage metric. The full load → persist path runs against live AGE in
``tests/integration/test_anchor.py`` (CI).
"""

import uuid
from datetime import UTC, datetime

import pytest

from iknos.core.anchor import (
    ANCHOR_CANDIDATE_BAR,
    ANCHOR_CONFIRM_BAR,
    AnchorCoverage,
    TaxonomyNode,
    anchors_to_props,
    block_anchors,
    decide_anchor,
    score_anchor,
)
from iknos.core.extract import NodeKind
from iknos.core.reference import Referent
from iknos.types.edges import AnchorState

# --- helpers ---


def _entity(label: str, *, type: str = "", kind: NodeKind = NodeKind.OBJECT) -> Referent:
    """A canonical case referent (single fresh node) with the given surface."""
    eid = uuid.uuid4()
    return Referent(canonical=eid, ids=frozenset({eid}), label=label, type=type, kind=kind)


def _node(label: str, *, type: str = "", id: uuid.UUID | None = None) -> TaxonomyNode:
    return TaxonomyNode(id=id or uuid.uuid4(), box=uuid.uuid4(), label=label, type=type)


# --- blocking ---


def test_block_anchors_shares_token():
    pump = _node("Centrifugal pump")
    housing = _node("Pump housing")
    bearing = _node("Rolling-element bearing")
    blocked = block_anchors(_entity("pump"), [pump, housing, bearing])
    assert set(blocked) == {pump, housing}  # both share "pump"; bearing shares nothing


def test_block_anchors_empty_label():
    # A punctuation-only surface normalizes to empty → no token → blocks to nothing.
    assert block_anchors(_entity("---"), [_node("Roller")]) == []


def test_block_anchors_no_overlap():
    assert block_anchors(_entity("gearbox"), [_node("Roller"), _node("Pump")]) == []


# --- scoring ---


def test_score_exact_reaches_confirm_bar():
    # Exact normalized-label match is the controlled-vocabulary anchor signal — alone confirms.
    assert score_anchor(_entity("roller"), _node("Roller")) >= ANCHOR_CONFIRM_BAR


def test_score_containment_is_candidate_band():
    # "pump" is a shorter form of "Centrifugal pump" — containment, not exact → candidate band.
    s = score_anchor(_entity("pump"), _node("Centrifugal pump"))
    assert ANCHOR_CANDIDATE_BAR <= s < ANCHOR_CONFIRM_BAR


def test_score_no_overlap_is_zero():
    assert score_anchor(_entity("gearbox"), _node("Roller")) == 0.0


def test_score_type_agreement_is_a_bonus_not_required():
    # Agreeing type nudges the score up but is never required (exact already confirms),
    # and a mismatch never drops below the exact-match contribution.
    agree = score_anchor(_entity("roller", type="Component"), _node("Roller", type="Component"))
    mismatch = score_anchor(_entity("roller", type="widget"), _node("Roller", type="Component"))
    assert agree > mismatch
    assert mismatch >= ANCHOR_CONFIRM_BAR  # exact label alone still confirms


def test_score_partial_containment_below_candidate():
    # Half the tokens shared, no exact match → below the candidate bar (no anchor).
    assert (
        score_anchor(_entity("high speed bearing"), _node("bearing housing")) < ANCHOR_CANDIDATE_BAR
    )


# --- decision ---


def test_decide_single_exact_confirms():
    roller = _node("Roller")
    other = _node("Roller bearing")  # contains "roller" but not exact → candidate band
    d = decide_anchor(_entity("roller"), [roller, other])
    assert d.state is AnchorState.CONFIRMED
    assert d.targets == [roller]
    assert d.anchored is True


def test_decide_tie_stays_candidate():
    # A cross-pack-style homonym: two nodes tie on containment-only → open candidates, not a
    # forced confirm.
    pump = _node("Centrifugal pump")
    housing = _node("Pump housing")
    d = decide_anchor(_entity("pump"), [pump, housing])
    assert d.state is AnchorState.CANDIDATE
    assert set(d.targets) == {pump, housing}
    assert d.anchored is False


def test_decide_partial_is_candidate():
    node = _node("Centrifugal pump")
    d = decide_anchor(_entity("pump"), [node])
    assert d.state is AnchorState.CANDIDATE
    assert d.targets == [node]  # single open candidate target


def test_decide_no_candidate_is_unresolved():
    d = decide_anchor(_entity("gearbox"), [_node("Roller"), _node("Pump")])
    assert d.state is None
    assert d.targets == []
    assert d.score == 0.0
    assert d.anchored is False


def test_decide_confirm_blocked_by_tie_with_another_top():
    # Two exact matches (an ambiguity the active-pack scope failed to remove) → candidate, even
    # though each clears the confirm bar individually.
    a = _node("Roller")
    b = _node("Roller")
    d = decide_anchor(_entity("roller"), [a, b])
    assert d.state is AnchorState.CANDIDATE
    assert set(d.targets) == {a, b}


# --- write contract ---


def test_anchors_to_props_shape():
    box, target = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 6, 11, tzinfo=UTC)
    props = anchors_to_props(
        box=box, target_box=target, state=AnchorState.CONFIRMED, strength=0.95, now=now
    )
    assert props["box"] == str(box)
    assert props["target_box"] == str(target)
    assert props["state"] == "confirmed"
    assert props["strength"] == 0.95
    # Two §12 annotations seeded; confidence tracks the strength.
    assert props["support_count"] == 1
    assert props["confidence"] == 0.95
    # Bitemporal stamped open.
    assert props["valid_from"] == now.isoformat()
    assert props["valid_to"] is None
    assert props["event_time"] is None


# --- coverage metric ---


@pytest.mark.parametrize(
    "total,anchored,expected",
    [(0, 0, 0.0), (4, 1, 0.25), (3, 3, 1.0)],
)
def test_coverage_fraction(total, anchored, expected):
    assert AnchorCoverage(total=total, anchored=anchored).fraction == expected
