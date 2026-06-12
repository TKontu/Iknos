"""W1 integration test — the composed loop runs against real AGE end-to-end (§7.2, §12, §13).

Real Postgres+AGE. Seeds a small belief graph (base fact → conclusion → hypothesis, plus an
overturning refuter) and drives :meth:`RevisionLoop.run`, asserting the §12 feedback actually
fires: an *authorised* refutation retracts a supporting fact, Layer A drops the conclusion it
grounded, and the loop re-runs A → B → QBAF to a fixpoint that is then persisted. Gate decisions
are built through the **real** ``ensemble_gate.authorise`` (the spec: don't mock the gate). The DB
is reset before each test (conftest ``_isolate_db``), so these are exact assertions.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.ensemble_gate import DEFAULT_GATE, GateChannel, affirming, authorise
from iknos.core.qbaf_adapter import QbafAdapter
from iknos.core.revision_loop import RevisionLoop, no_decisions
from iknos.db.age import bootstrap_session, execute_cypher, merge_edge, merge_vertex, unquote_agtype
from iknos.types.intentional import HypothesisState
from iknos.types.nodes import Box

pytestmark = pytest.mark.asyncio

_AUTHORISED = authorise(
    [affirming(GateChannel.LLM), affirming(GateChannel.SYMBOLIC)], gate=DEFAULT_GATE
)


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_node(
    session: AsyncSession, label: str, box: uuid.UUID, *, confidence: float
) -> uuid.UUID:
    nid = uuid.uuid4()
    await merge_vertex(
        session,
        label,
        {"id": str(nid), "box": str(box), "confidence": confidence, "valid_to": None},
    )
    return nid


async def _evidence(session: AsyncSession, fact: uuid.UUID) -> None:
    """Give a node an ``EVIDENCED_BY`` Proposition so Layer A counts it as a base fact (§12)."""
    pid = uuid.uuid4()
    await merge_vertex(session, "Proposition", {"id": str(pid), "text": "claim"})
    await merge_edge(session, src_id=fact, dst_id=pid, label="EVIDENCED_BY", props={})


async def _derive(session: AsyncSession, *, conclusion: uuid.UUID, antecedent: uuid.UUID) -> None:
    await merge_edge(
        session,
        src_id=conclusion,
        dst_id=antecedent,
        label="DERIVED_FROM",
        props={"derivation": f"d-{conclusion}", "strength": 1.0, "valid_to": None},
    )


async def _evidential(
    session: AsyncSession,
    *,
    source: uuid.UUID,
    target: uuid.UUID,
    box: uuid.UUID,
    label: str,
    strength: float,
) -> None:
    await merge_edge(
        session,
        src_id=source,
        dst_id=target,
        label=label,
        props={"box": str(box), "strength": strength, "significance": 1.0, "valid_to": None},
    )


async def _read(session: AsyncSession, nid: uuid.UUID) -> dict[str, str]:
    rows = await execute_cypher(
        session,
        f"MATCH (n {{id: '{nid}'}}) RETURN n.state, n.valid_to, n.confidence, n.pending_refutation",
        returns="state agtype, valid_to agtype, conf agtype, pending agtype",
    )
    state, valid_to, conf, pending = rows[0]
    return {
        "state": unquote_agtype(state),
        "valid_to": unquote_agtype(valid_to),
        "confidence": str(conf),
        "pending": str(pending),
    }


async def _seed(
    session: AsyncSession, name: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """f (base) -> c (conclusion) --SUPPORTS--> h; r (base) --REFUTES--> h, the refuter dominating.

    h's base is low and r's attack (0.9) dominates c's weak support (0.2), so the QBAF computes h
    **refuted** from the start — the gate then has a refutation to authorise. c is grounded *only*
    by f, so retracting f is what drops c (Layer A) and drives the loop's second pass.
    """
    box = case_box(name, "1", "test", 0.8)
    await _put_box(session, box)
    f = await _put_node(session, "Fact", box.id, confidence=1.0)
    c = await _put_node(session, "DeductiveConclusion", box.id, confidence=1.0)
    h = await _put_node(session, "Hypothesis", box.id, confidence=0.3)
    r = await _put_node(session, "Fact", box.id, confidence=1.0)
    await _evidence(session, f)
    await _evidence(session, r)
    await _derive(session, conclusion=c, antecedent=f)  # c grounded only by f
    await _evidential(session, source=c, target=h, box=box.id, label="SUPPORTS", strength=0.2)
    await _evidential(session, source=r, target=h, box=box.id, label="REFUTES", strength=0.9)
    await session.commit()
    return box.id, f, c, h, r


async def test_authorised_refutation_retracts_a_supporting_fact_and_reconverges(
    session: AsyncSession,
) -> None:
    await bootstrap_session(session)
    _box, f, c, h, _r = await _seed(session, "w1-converge")

    # The §12 belief-revision policy for this fixture: an authorised refutation of h retracts the
    # base fact f that grounds h's supporter c. Layer A then drops c, and the loop re-adjudicates.
    def revise(verdicts, decisions, retracted):  # noqa: ANN001
        flip = any(
            v.state is HypothesisState.REFUTED
            and decisions.get(v.id)
            and decisions[v.id].authorised
            for v in verdicts
        )
        return (retracted | {str(f)}) if flip else retracted

    def decide(verdicts):  # noqa: ANN001
        return {v.id: _AUTHORISED for v in verdicts if v.state is HypothesisState.REFUTED}

    result = await RevisionLoop().run(session, decide=decide, revise=revise, max_iterations=10)

    assert result.converged
    assert result.retracted == frozenset({str(f)})
    # The loop genuinely re-ran (retract f → re-derive → re-adjudicate), not a single pass.
    assert result.stabilization.iterations >= 2
    assert len(result.action_ids) >= 2  # one Action per iteration (§10.1)

    fr, cr, hr = await _read(session, f), await _read(session, c), await _read(session, h)
    assert fr["valid_to"] != "null"  # f retracted (the supporting fact)
    assert cr["valid_to"] == "null" and float(cr["confidence"]) == pytest.approx(
        0.0
    )  # c ungrounded
    assert hr["state"] == "refuted"  # h's authorised flip persisted (V8)
    assert hr["pending"] == "false"

    # A fresh, independent adjudication agrees — the loop left the graph consistent.
    adj = await QbafAdapter().evaluate(session)
    assert all(v.state is HypothesisState.REFUTED for v in adj.verdicts if v.id == str(h))


async def test_unauthorised_refutation_is_held_not_retracted(session: AsyncSession) -> None:
    """With no gate decision the structural refutation is held (V8): nothing retracted, the loop
    converges in one pass, and h keeps its prior state with ``pending_refutation`` set."""
    await bootstrap_session(session)
    _box, f, _c, h, _r = await _seed(session, "w1-hold")

    result = await RevisionLoop().run(session, decide=no_decisions, max_iterations=10)

    assert result.converged
    assert result.retracted == frozenset()  # nothing authorised → nothing retracted
    assert result.is_finding  # the held refutation is surfaced (pending_refutation, §13)

    fr, hr = await _read(session, f), await _read(session, h)
    assert fr["valid_to"] == "null"  # the supporting fact stands
    assert hr["state"] != "refuted"  # the flip was withheld
    assert hr["pending"] == "true"
