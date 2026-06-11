"""G3.8 integration test — the deduce/induce operators end to end against real AGE.

Real Postgres+AGE. Seeds base ``Fact``s (``EVIDENCED_BY`` Spans) in a case box, derives a
``DeductiveConclusion`` into a working box, and asserts: the conclusion node + its
``DERIVED_FROM`` group are written; the two §12 annotations are **computed by Layer A/B**
(confidence is the weakest link, not a raw input); the ``Action`` is joinable with the
premises' source spans (§10.2); the adapter then re-reads the conclusion as supported; and
retracting the sole supporting premise drops the conclusion's support. Also checks ``induce``
marks its conclusion provisional, and chaining a conclusion onto another conclusion.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box, working_box
from iknos.core.derivation_adapter import DerivationGraphAdapter, support_and_confidence
from iknos.core.derive import Deriver
from iknos.db.age import bootstrap_session, execute_cypher, merge_edge, merge_vertex
from iknos.types.nodes import Box

pytestmark = pytest.mark.asyncio


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_fact(session: AsyncSession, box: uuid.UUID, *, confidence: float) -> uuid.UUID:
    """A base Fact (EVIDENCED_BY a Span) — a premise the deriver can build on."""
    fid, sid = uuid.uuid4(), uuid.uuid4()
    await merge_vertex(
        session,
        "Fact",
        {"id": str(fid), "box": str(box), "confidence": confidence, "valid_to": None},
    )
    await merge_vertex(
        session, "Span", {"id": str(sid), "document_id": str(uuid.uuid4()), "start": 0, "end": 5}
    )
    await merge_edge(session, src_id=fid, dst_id=sid, label="EVIDENCED_BY", props={"box": str(box)})
    return fid


async def _retract_node(session: AsyncSession, nid: uuid.UUID) -> None:
    now = datetime.now(UTC).isoformat()
    await execute_cypher(session, f"MATCH (n {{id: '{nid}'}}) SET n.valid_to = '{now}'")


async def _node_props(session: AsyncSession, label: str, nid: uuid.UUID) -> dict:
    rows = await execute_cypher(
        session,
        f"MATCH (n:{label} {{id: '{nid}'}}) RETURN n.provisional, n.support_count, n.confidence",
        returns="prov agtype, sc agtype, conf agtype",
    )
    prov, sc, conf = rows[0]
    return {"provisional": str(prov), "support_count": int(str(sc)), "confidence": float(str(conf))}


async def test_deduce_writes_a_certified_valued_conclusion(session: AsyncSession) -> None:
    await bootstrap_session(session)
    case = case_box("g38-case", "1", "test", 0.8)
    work = working_box("g38-work", "1", "reasoning", 1.0)
    await _put_box(session, case)
    await _put_box(session, work)

    f1 = await _put_fact(session, case.id, confidence=0.6)
    f2 = await _put_fact(session, case.id, confidence=0.9)
    await session.commit()

    deriver = Deriver()
    result = await deriver.deduce(session, "the unit is degraded", (f1, f2), work, strength=1.0)

    # Annotations are computed by the engine: Layer A support_count 1, Layer B Gödel
    # weakest-link confidence = min(strength 1.0, f1 0.6, f2 0.9) = 0.6 — not a raw input.
    assert result.support_count == 1
    assert result.confidence == pytest.approx(0.6)

    props = await _node_props(session, "DeductiveConclusion", result.conclusion_id)
    assert props["provisional"] == "false"
    assert props["support_count"] == 1
    assert props["confidence"] == pytest.approx(0.6)

    # The DERIVED_FROM group: two edges, shared group id, the step strength.
    edges = await execute_cypher(
        session,
        f"MATCH (c:DeductiveConclusion {{id: '{result.conclusion_id}'}})-[d:DERIVED_FROM]->(a) "
        "RETURN a.id, d.derivation, d.strength",
        returns="aid agtype, grp agtype, strength agtype",
    )
    assert len(edges) == 2
    groups = {str(g).strip('"') for _, g, _ in edges}
    assert groups == {str(result.derivation_group)}
    assert {str(a).strip('"') for a, _, _ in edges} == {str(f1), str(f2)}

    # The derive Action is joinable and records the premises' source spans (§10.2).
    act = await session.execute(
        text(
            "SELECT inputs, outputs FROM actions WHERE actor='deriver' "
            "AND outputs->>'conclusion' = :c"
        ),
        {"c": str(result.conclusion_id)},
    )
    rec = act.one()
    assert set(rec.inputs["premises"]) == {str(f1), str(f2)}
    assert str(f1) in rec.inputs["spans"]  # premise -> its EVIDENCED_BY span recorded
    assert rec.outputs["confidence"] == pytest.approx(0.6)

    # The adapter re-reads the conclusion as supported, scored by Layer B.
    sub = await DerivationGraphAdapter().load_active(session)
    supported, conf = support_and_confidence(sub)
    cs = str(result.conclusion_id)
    assert cs in supported
    assert conf[cs] == pytest.approx(0.6)


async def test_retracting_sole_premise_unsupports_the_conclusion(session: AsyncSession) -> None:
    await bootstrap_session(session)
    case = case_box("g38-retract-case", "1", "test", 0.8)
    work = working_box("g38-retract-work", "1", "reasoning", 1.0)
    await _put_box(session, case)
    await _put_box(session, work)
    f1 = await _put_fact(session, case.id, confidence=1.0)
    await session.commit()

    deriver = Deriver()
    result = await deriver.deduce(session, "follows from f1", (f1,), work)
    cs = str(result.conclusion_id)

    sub = await DerivationGraphAdapter().load_active(session)
    supported, _ = support_and_confidence(sub)
    assert cs in supported  # grounded while its premise stands

    await _retract_node(session, f1)
    await session.commit()
    sub2 = await DerivationGraphAdapter().load_active(session)
    supported2, conf2 = support_and_confidence(sub2)
    assert cs not in supported2  # premise gone -> conclusion unfounded
    assert cs not in conf2


async def test_induce_marks_provisional_and_chains(session: AsyncSession) -> None:
    await bootstrap_session(session)
    case = case_box("g38-induce-case", "1", "test", 0.8)
    work = working_box("g38-induce-work", "1", "reasoning", 1.0)
    await _put_box(session, case)
    await _put_box(session, work)
    f1 = await _put_fact(session, case.id, confidence=0.9)
    await session.commit()

    deriver = Deriver()
    # induce a provisional generalization from f1 (step strength 0.7).
    ind = await deriver.induce(session, "pumps of this type degrade", (f1,), work, strength=0.7)
    props = await _node_props(session, "InductiveConclusion", ind.conclusion_id)
    assert props["provisional"] == "true"
    # Gödel: conf = min(0.7, 0.9) = 0.7.
    assert props["confidence"] == pytest.approx(0.7)

    # Chain: deduce a further conclusion FROM the inductive conclusion (premises may be
    # conclusions, §6). conf = min(1.0, 0.7) = 0.7.
    chained = await deriver.deduce(session, "so maintenance is due", (ind.conclusion_id,), work)
    assert chained.confidence == pytest.approx(0.7)
    sub = await DerivationGraphAdapter().load_active(session)
    supported, conf = support_and_confidence(sub)
    assert {str(ind.conclusion_id), str(chained.conclusion_id)} <= supported
