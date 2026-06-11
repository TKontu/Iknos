"""G3.8 — unit tests for the derivation operators' pure paths (DB-free).

The DB write path of :class:`Deriver` is exercised by the integration test against real AGE;
here we pin the **write contracts** (``conclusion_to_props``, ``derivation_edge_props``) and
the **engine annotation computation** (``value_conclusion``) — the "engine disposes" half:
membership from Layer A, confidence from Layer B, foundedness gating both — with hand-built
subgraphs, exactly like the truth-maintenance / confidence unit tests.
"""

import uuid
from datetime import UTC, datetime

from iknos.boxes.serde import working_box
from iknos.core.confidence import GODEL, VITERBI
from iknos.core.derivation_adapter import (
    DerivedRow,
    NodeRow,
    assemble_subgraph,
)
from iknos.core.derive import (
    DerivationKind,
    DerivationProposal,
    conclusion_to_props,
    derivation_edge_props,
    value_conclusion,
)
from iknos.core.truth_maintenance import Derivation
from iknos.types.annotations import Annotations
from iknos.types.nodes import Conclusion, Tier
from iknos.types.temporal import BitemporalFields


def _subgraph(*facts: tuple[str, float]):
    """An active subgraph of base facts only (no derivations) with given confidences."""
    nodes = [NodeRow(id=i, box="bx", confidence=c) for i, c in facts]
    return assemble_subgraph(nodes, [i for i, _ in facts], [], active_box_ids=frozenset({"bx"}))


def test_value_conclusion_grounded_deductive_takes_weakest_link() -> None:
    # A(0.6), B(0.9) -> C, deductive strength 1.0. Gödel: conf(C) = min(0.6, 0.9) = 0.6.
    sub = _subgraph(("A", 0.6), ("B", 0.9))
    deriv = Derivation("C", frozenset({"A", "B"}))
    sc, conf = value_conclusion(sub, deriv, 1.0, semiring=GODEL)
    assert sc == 1
    assert conf == 0.6


def test_value_conclusion_step_strength_discounts_under_godel() -> None:
    # A weak inference step (0.4) caps the conclusion below the premises' confidence.
    sub = _subgraph(("A", 0.9))
    deriv = Derivation("C", frozenset({"A"}))
    sc, conf = value_conclusion(sub, deriv, 0.4, semiring=GODEL)
    assert sc == 1
    assert conf == 0.4


def test_value_conclusion_viterbi_multiplies() -> None:
    sub = _subgraph(("A", 0.9))
    deriv = Derivation("C", frozenset({"A"}))
    _, conf = value_conclusion(sub, deriv, 0.8, semiring=VITERBI)
    assert abs(conf - 0.72) < 1e-9


def test_value_conclusion_ungrounded_premise_yields_zero() -> None:
    # 'X' is not a loaded/supported node, so C is not well-founded -> (0, 0.0).
    sub = _subgraph(("A", 1.0))
    deriv = Derivation("C", frozenset({"A", "X"}))
    sc, conf = value_conclusion(sub, deriv, 1.0)
    assert sc == 0
    assert conf == 0.0


def test_value_conclusion_chains_on_an_existing_conclusion() -> None:
    # Subgraph already has A -> C1 (a prior conclusion). Now derive C2 from C1.
    nodes = [
        NodeRow(id="A", box="bx", confidence=0.8),
        NodeRow(id="C1", box="bx", confidence=1.0),
    ]
    sub = assemble_subgraph(
        nodes, ["A"], [DerivedRow("C1", "A", "g1", 0.9)], active_box_ids=frozenset({"bx"})
    )
    deriv = Derivation("C2", frozenset({"C1"}))
    sc, conf = value_conclusion(sub, deriv, 1.0, semiring=GODEL)
    # conf(C1) = min(0.9, 0.8) = 0.8; conf(C2) = min(1.0, 0.8) = 0.8.
    assert sc == 1
    assert conf == 0.8


def test_conclusion_to_props_round_trips_the_write_contract() -> None:
    now = datetime(2026, 6, 11, tzinfo=UTC)
    c = Conclusion(
        id=uuid.uuid4(),
        box=uuid.uuid4(),
        tier=Tier.WORKING,
        statement="the pump failed",
        provisional=True,
        annotations=Annotations(support_count=2, confidence=0.7),
        temporal=BitemporalFields(ingested_at=now, valid_from=now),
    )
    props = conclusion_to_props(c)
    assert props["statement"] == "the pump failed"
    assert props["provisional"] is True
    assert props["support_count"] == 2
    assert props["confidence"] == 0.7
    assert props["tier"] == str(Tier.WORKING)
    assert props["valid_to"] is None
    assert props["id"] == str(c.id)
    assert "override" not in props  # null soft-override omitted, not written


def test_derivation_edge_props_carries_group_and_strength() -> None:
    now = datetime(2026, 6, 11, tzinfo=UTC)
    box, group = uuid.uuid4(), uuid.uuid4()
    props = derivation_edge_props(box=box, group=group, strength=0.85, now=now)
    assert props["derivation"] == str(group)
    assert props["strength"] == 0.85
    assert props["box"] == str(box)
    assert props["valid_to"] is None
    assert props["ingested_at"] == now.isoformat()


def test_proposal_kind_maps_to_provisional() -> None:
    ded = DerivationProposal("s", (uuid.uuid4(),), DerivationKind.DEDUCTIVE, 1.0)
    ind = DerivationProposal("s", (uuid.uuid4(),), DerivationKind.INDUCTIVE, 0.5)
    assert ded.kind is DerivationKind.DEDUCTIVE
    assert ind.kind is DerivationKind.INDUCTIVE


def test_working_box_is_working_tier_and_active() -> None:
    box = working_box("inv-1", "1", "reasoning", 1.0)
    assert box.tier is Tier.WORKING
    assert box.status.value == "active"
