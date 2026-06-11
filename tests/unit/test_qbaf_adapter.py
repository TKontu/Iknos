"""G4.4 — the QBAF adapter's pure core: row assembly + adjudication read-off (DB-free).

Mirrors ``test_derivation_adapter.py`` (the G3.4 unit tests): the grouping/filtering and the
evaluate step are exercised with hand-built rows, leaving the AGE reads/writes to the
integration test. Covers sign routing, active-box gating, dead-endpoint drop, the §12 base-score
seam, the computed verdict (supported / refuted / unsupported), and the §7.2 persist gate's pure
decision (:func:`refutation_held`, V8) over real :func:`~iknos.core.ensemble_gate.authorise`
decisions.
"""

import pytest

from iknos.core.derivation_adapter import NodeRow
from iknos.core.ensemble_gate import (
    DEFAULT_GATE,
    GateChannel,
    affirming,
    authorise,
    dissenting,
)
from iknos.core.qbaf_adapter import (
    EvidenceRow,
    adjudicate,
    assemble_baf,
    refutation_held,
)
from iknos.types.edges import EdgeSign
from iknos.types.intentional import AcceptabilityBand, HypothesisState


def _node(nid: str, *, box: str | None = "b1", confidence: float = 1.0) -> NodeRow:
    return NodeRow(id=nid, box=box, confidence=confidence)


# --------------------------------------------------------------------------------------------
# assemble_baf — arguments, base map, sign routing, active-box + dead-endpoint filtering
# --------------------------------------------------------------------------------------------


def test_assemble_builds_arguments_base_map_and_routes_by_sign() -> None:
    nodes = [_node("h", confidence=0.3), _node("f1", confidence=0.9), _node("f2", confidence=0.7)]
    edges = [
        EvidenceRow(source="f1", target="h", sign=EdgeSign.SUPPORTS, strength=0.8),
        EvidenceRow(source="f2", target="h", sign=EdgeSign.REFUTES, strength=0.4),
    ]
    out = assemble_baf(nodes, edges)
    assert out.baf.arguments == frozenset({"h", "f1", "f2"})
    assert out.base == pytest.approx({"h": 0.3, "f1": 0.9, "f2": 0.7})  # = Layer B confidence
    assert [(e.src, e.dst, e.strength) for e in out.baf.supports] == [("f1", "h", 0.8)]
    assert [(e.src, e.dst, e.strength) for e in out.baf.attacks] == [("f2", "h", 0.4)]


def test_inactive_box_node_is_excluded_and_its_edge_dropped() -> None:
    """A node in a non-active box is not an argument, and an edge resting on it is dropped — a
    deprecated-box supporter lends nothing (additive support, the opposite of the derivation
    adapter keeping an inactive antecedent in a conjunctive body)."""
    nodes = [_node("h", box="active"), _node("dead", box="deprecated", confidence=1.0)]
    edges = [EvidenceRow(source="dead", target="h", sign=EdgeSign.SUPPORTS, strength=1.0)]
    out = assemble_baf(nodes, edges, active_box_ids=frozenset({"active"}))
    assert out.baf.arguments == frozenset({"h"})
    assert out.baf.supports == ()  # the dead supporter's edge dropped


def test_dangling_edge_to_unknown_node_is_dropped() -> None:
    nodes = [_node("h"), _node("f1")]
    edges = [
        EvidenceRow(source="f1", target="h", sign=EdgeSign.SUPPORTS, strength=0.6),
        EvidenceRow(source="ghost", target="h", sign=EdgeSign.SUPPORTS, strength=1.0),
    ]
    out = assemble_baf(nodes, edges)
    assert [(e.src, e.dst) for e in out.baf.supports] == [("f1", "h")]


def test_assemble_is_deterministic() -> None:
    nodes = [_node("h"), _node("a"), _node("b")]
    edges = [
        EvidenceRow(source="b", target="h", sign=EdgeSign.SUPPORTS, strength=0.5),
        EvidenceRow(source="a", target="h", sign=EdgeSign.SUPPORTS, strength=0.5),
    ]
    first = assemble_baf(nodes, edges).baf
    second = assemble_baf(list(reversed(nodes)), list(reversed(edges))).baf
    assert first == second


# --------------------------------------------------------------------------------------------
# adjudicate — acceptability over all args; computed state/band for hypotheses only
# --------------------------------------------------------------------------------------------


def test_adjudicate_supported_hypothesis() -> None:
    """Strong support lifts a hypothesis above its base; state SUPPORTED, band high (DF-QuAD)."""
    nodes = [_node("h", confidence=0.3), _node("f1", confidence=1.0)]
    edges = [EvidenceRow(source="f1", target="h", sign=EdgeSign.SUPPORTS, strength=0.8)]
    result = adjudicate(assemble_baf(nodes, edges), ["h"])
    # acceptability covers every argument (the supporting fact too).
    assert result.acceptability["f1"] == pytest.approx(1.0)
    (v,) = result.verdicts
    assert v.id == "h"
    assert v.acceptability == pytest.approx(0.3 + 0.7 * 0.8)  # combine(0.3, 0.8, 0) = 0.86
    assert v.state is HypothesisState.SUPPORTED
    assert v.band is AcceptabilityBand.TRUE
    assert result.converged


def test_adjudicate_refuted_hypothesis() -> None:
    """Net attack pulls a hypothesis below its base; state REFUTED (the engine's finding —
    persisting the flip is ensemble-gated, §7.2/G4.5)."""
    nodes = [_node("h", confidence=0.5), _node("f1", confidence=1.0)]
    edges = [EvidenceRow(source="f1", target="h", sign=EdgeSign.REFUTES, strength=0.9)]
    (v,) = adjudicate(assemble_baf(nodes, edges), ["h"]).verdicts
    assert v.acceptability == pytest.approx(0.5 - 0.5 * 0.9)  # combine(0.5, 0, 0.9) = 0.05
    assert v.state is HypothesisState.REFUTED
    assert v.band is AcceptabilityBand.FALSE


def test_adjudicate_unsupported_hypothesis_has_no_active_evidence() -> None:
    nodes = [_node("h", confidence=0.3)]
    (v,) = adjudicate(assemble_baf(nodes, []), ["h"]).verdicts
    assert v.acceptability == pytest.approx(0.3)  # stays at its base — no evidence
    assert v.state is HypothesisState.UNSUPPORTED
    assert v.band is AcceptabilityBand.IMPLAUSIBLE


def test_adjudicate_skips_a_hypothesis_outside_the_active_subgraph() -> None:
    """A hypothesis id not present in the loaded subgraph yields no verdict (nothing to score)."""
    out = adjudicate(assemble_baf([_node("h")], []), ["h", "gone"])
    assert {v.id for v in out.verdicts} == {"h"}


# --------------------------------------------------------------------------------------------
# §7.2 persist gate — refutation_held over real ensemble decisions (V8)
# --------------------------------------------------------------------------------------------

# GateDecisions built through the *real* authorise (the spec: don't mock the gate). DEFAULT_GATE
# requires {LLM, SYMBOLIC}; a dissent vetoes under every policy.
_AUTHORISED = authorise(
    [affirming(GateChannel.LLM), affirming(GateChannel.SYMBOLIC)], gate=DEFAULT_GATE
)
_WITHHELD = authorise([affirming(GateChannel.LLM)], gate=DEFAULT_GATE)  # SYMBOLIC required, absent
_VETOED = authorise(
    [affirming(GateChannel.LLM), dissenting(GateChannel.SYMBOLIC)], gate=DEFAULT_GATE
)


def test_decisions_are_what_we_think() -> None:
    # Sanity on the real gate so the held-table below means what it says.
    assert _AUTHORISED.authorised is True
    assert _WITHHELD.is_finding is True and not _WITHHELD.authorised
    assert _VETOED.is_finding is True and not _VETOED.authorised


def test_refutation_held_only_for_a_refuted_state() -> None:
    # A non-refuted verdict is never held, regardless of any decision (the gate only gates flips).
    for state in (HypothesisState.SUPPORTED, HypothesisState.UNSUPPORTED):
        assert refutation_held(state, None) is False
        assert refutation_held(state, _WITHHELD) is False
        assert refutation_held(state, _AUTHORISED) is False


def test_refutation_held_truth_table_for_refuted() -> None:
    # refuted + authorising → flip allowed (not held); refuted + withheld/vetoed/no-decision → held.
    assert refutation_held(HypothesisState.REFUTED, _AUTHORISED) is False
    assert refutation_held(HypothesisState.REFUTED, _WITHHELD) is True
    assert refutation_held(HypothesisState.REFUTED, _VETOED) is True
    # No decision at all (no ensemble ran) is treated as not-authorised — withheld, never defaulted.
    assert refutation_held(HypothesisState.REFUTED, None) is True


def test_persist_result_surfaces_held_as_a_finding() -> None:
    from iknos.core.qbaf_adapter import (
        PENDING_REFUTATION_REASON,
        HeldRefutation,
        PersistResult,
    )

    held = HeldRefutation(id="h1", held_state=HypothesisState.UNSUPPORTED, decision=_WITHHELD)
    assert held.reason == PENDING_REFUTATION_REASON
    assert PersistResult(written=1, held=(held,)).is_finding is True
    assert PersistResult(written=2).is_finding is False
