"""G3.4 integration test — the Phase-2 adapter reads real AGE into the reasoning core.

Real Postgres+AGE. Seeds an active box with base ``Fact``s (``EVIDENCED_BY`` a Proposition,
each with a ``confidence`` seed) and a ``DeductiveConclusion`` grounded by a ``DERIVED_FROM``
group, plus a deprecated-box fact and a retracted fact, then asserts
:meth:`DerivationGraphAdapter.load_active` reconstructs the derivation graph + the Layer B
side maps and that the two-layer seam (Layer A certifies → Layer B scores) runs over it.

``load_active`` reads the whole active subgraph, so on the shared CI/dev DB these assertions
are **containment** (my seeded nodes are present / absent), never global equality.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.derivation_adapter import DerivationGraphAdapter, support_and_confidence
from iknos.core.truth_maintenance import Derivation
from iknos.db.age import bootstrap_session, execute_cypher, merge_edge, merge_vertex
from iknos.types.nodes import Box, BoxStatus, Tier

pytestmark = pytest.mark.asyncio


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_fact(
    session: AsyncSession, box: uuid.UUID, *, confidence: float, valid_to: str | None = None
) -> uuid.UUID:
    """A base Fact: a reasoning node with a confidence seed, EVIDENCED_BY a Proposition."""
    fid = uuid.uuid4()
    pid = uuid.uuid4()
    await merge_vertex(
        session,
        "Fact",
        {"id": str(fid), "box": str(box), "confidence": confidence, "valid_to": valid_to},
    )
    await merge_vertex(session, "Proposition", {"id": str(pid), "text": "p"})
    await merge_edge(session, src_id=fid, dst_id=pid, label="EVIDENCED_BY", props={"box": str(box)})
    return fid


async def _put_conclusion(
    session: AsyncSession,
    box: uuid.UUID,
    *,
    antecedents: list[uuid.UUID],
    strength: float,
    confidence: float = 1.0,
) -> uuid.UUID:
    """A DeductiveConclusion grounded by one DERIVED_FROM group over ``antecedents``."""
    cid = uuid.uuid4()
    group = str(uuid.uuid4())
    await merge_vertex(
        session,
        "DeductiveConclusion",
        {"id": str(cid), "box": str(box), "confidence": confidence, "valid_to": None},
    )
    for a in antecedents:
        await merge_edge(
            session,
            src_id=cid,
            dst_id=a,
            label="DERIVED_FROM",
            props={"box": str(box), "derivation": group, "strength": strength, "valid_to": None},
        )
    return cid


async def _retract_node(session: AsyncSession, nid: uuid.UUID) -> None:
    """Stamp ``valid_to`` on a node — the bitemporal retraction the adapter filters on."""
    now = datetime.now(UTC).isoformat()
    await execute_cypher(
        session,
        f"MATCH (n {{id: '{nid}'}}) SET n.valid_to = '{now}'",
    )


async def test_adapter_loads_active_subgraph_and_runs_both_layers(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("g34-adapter", "1", "test", 0.8)
    await _put_box(session, box)

    # Two base facts -> one conclusion (conjunction, strength 0.8). f1 carries a 0.6 seed.
    f1 = await _put_fact(session, box.id, confidence=0.6)
    f2 = await _put_fact(session, box.id, confidence=0.9)
    concl = await _put_conclusion(session, box.id, antecedents=[f1, f2], strength=0.8)

    # A deprecated box with a fact that must be excluded by the active-box filter.
    dead = case_box("g34-dead", "1", "test", 0.5)
    dead = dead.model_copy(update={"status": BoxStatus.DEPRECATED})
    assert dead.tier is Tier.CASE
    await _put_box(session, dead)
    dead_fact = await _put_fact(session, dead.id, confidence=1.0)

    # A retracted (valid_to stamped) fact in the active box must be excluded too.
    gone = await _put_fact(session, box.id, confidence=1.0)
    await _retract_node(session, gone)
    await session.commit()

    sub = await DerivationGraphAdapter().load_active(session)

    f1s, f2s, cs = str(f1), str(f2), str(concl)
    # Base facts: f1, f2 present; the deprecated-box and retracted facts absent.
    assert {f1s, f2s} <= sub.graph.base_facts
    assert str(dead_fact) not in sub.graph.base_facts
    assert str(gone) not in sub.graph.base_facts
    # The conclusion is not a base fact (it is derived, not evidenced).
    assert cs not in sub.graph.base_facts

    # The DERIVED_FROM group reassembled into one conjunctive Derivation with its strength.
    deriv = Derivation(conclusion=cs, body=frozenset({f1s, f2s}))
    assert deriv in sub.graph.derivations
    assert sub.strength[deriv] == pytest.approx(0.8)

    # Layer B base seeds came across from the Fact confidence properties.
    assert sub.base_confidence[f1s] == pytest.approx(0.6)
    assert sub.base_confidence[f2s] == pytest.approx(0.9)

    # The two-layer seam over real data: Layer A certifies f1,f2,concl; Gödel scores the
    # conclusion at the weakest link min(strength 0.8, f1 0.6, f2 0.9) = 0.6.
    supported, conf = support_and_confidence(sub)
    assert {f1s, f2s, cs} <= supported
    assert conf[cs] == pytest.approx(0.6)


async def test_retracting_a_base_fact_drops_its_conclusion(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("g34-retract", "1", "test", 0.8)
    await _put_box(session, box)

    f1 = await _put_fact(session, box.id, confidence=1.0)
    concl = await _put_conclusion(session, box.id, antecedents=[f1], strength=0.9)
    await session.commit()

    f1s, cs = str(f1), str(concl)
    sub = await DerivationGraphAdapter().load_active(session)
    supported, _ = support_and_confidence(sub)
    assert {f1s, cs} <= supported  # grounded before retraction

    # Retract the sole supporting fact; reload; the conclusion loses support.
    await _retract_node(session, f1)
    await session.commit()
    sub2 = await DerivationGraphAdapter().load_active(session)
    supported2, conf2 = support_and_confidence(sub2)
    assert f1s not in supported2
    assert cs not in supported2  # the conclusion is no longer founded
    assert cs not in conf2  # and so it receives no confidence (§12 foundedness gate)
