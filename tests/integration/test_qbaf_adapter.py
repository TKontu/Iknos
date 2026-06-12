"""G4.4 integration test — the QBAF adapter reads real AGE, adjudicates, and writes back.

Real Postgres+AGE. Seeds an active box with a ``Hypothesis`` and supporting/attacking ``Fact``s
(``SUPPORTS``/``REFUTES`` edges carrying §7.1 ``strength``), plus a deprecated-box supporter and
a retracted supporter that must be excluded, then asserts :meth:`QbafAdapter.evaluate`
reconstructs the framework and computes the verdict, and :meth:`QbafAdapter.persist_verdicts`
writes ``acceptability`` + ``state`` back to the node *without* clobbering its ``confidence``.

``evaluate`` reads the whole active subgraph, so on the shared CI/dev DB these assertions are
**containment** (my seeded hypothesis is present / my excluded nodes don't inflate it), never
global equality — like the G3.4 test.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.ensemble_gate import DEFAULT_GATE, GateChannel, affirming, authorise
from iknos.core.qbaf_adapter import QbafAdapter
from iknos.db.age import bootstrap_session, execute_cypher, merge_edge, merge_vertex, unquote_agtype
from iknos.types.intentional import AcceptabilityBand, HypothesisState
from iknos.types.nodes import Box, BoxStatus, Tier

pytestmark = pytest.mark.asyncio


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_fact(session: AsyncSession, box: uuid.UUID, *, confidence: float) -> uuid.UUID:
    fid = uuid.uuid4()
    await merge_vertex(
        session,
        "Fact",
        {"id": str(fid), "box": str(box), "confidence": confidence, "valid_to": None},
    )
    return fid


async def _put_hypothesis(session: AsyncSession, box: uuid.UUID, *, confidence: float) -> uuid.UUID:
    hid = uuid.uuid4()
    await merge_vertex(
        session,
        "Hypothesis",
        {"id": str(hid), "box": str(box), "confidence": confidence, "valid_to": None},
    )
    return hid


async def _put_evidence(
    session: AsyncSession,
    *,
    source: uuid.UUID,
    target: uuid.UUID,
    box: uuid.UUID,
    label: str,
    strength: float,
) -> None:
    """A SUPPORTS/REFUTES edge: evidence ``source`` → ``target`` hypothesis (§5, §10)."""
    await merge_edge(
        session,
        src_id=source,
        dst_id=target,
        label=label,
        props={"box": str(box), "strength": strength, "significance": 1.0, "valid_to": None},
    )


async def _retract_node(session: AsyncSession, nid: uuid.UUID) -> None:
    await execute_cypher(
        session, f"MATCH (n {{id: '{nid}'}}) SET n.valid_to = '2026-01-01T00:00:00'"
    )


async def _read_hypothesis(session: AsyncSession, hid: uuid.UUID) -> tuple[str, str, str]:
    """Read back (acceptability, state, confidence) of a Hypothesis as raw agtype strings."""
    rows = await execute_cypher(
        session,
        f"MATCH (h:Hypothesis {{id: '{hid}'}}) RETURN h.acceptability, h.state, h.confidence",
        returns="acc agtype, state agtype, conf agtype",
    )
    acc, state, conf = rows[0]
    return str(acc), unquote_agtype(state), str(conf)


async def test_evaluate_computes_verdict_and_excludes_dead_evidence(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("g44-qbaf", "1", "test", 0.8)
    await _put_box(session, box)

    # h supported by f1 (0.8) and f2 (0.6), lightly attacked by g (0.3).
    h = await _put_hypothesis(session, box.id, confidence=0.4)
    f1 = await _put_fact(session, box.id, confidence=1.0)
    f2 = await _put_fact(session, box.id, confidence=1.0)
    g = await _put_fact(session, box.id, confidence=1.0)
    await _put_evidence(session, source=f1, target=h, box=box.id, label="SUPPORTS", strength=0.8)
    await _put_evidence(session, source=f2, target=h, box=box.id, label="SUPPORTS", strength=0.6)
    await _put_evidence(session, source=g, target=h, box=box.id, label="REFUTES", strength=0.3)

    # A deprecated-box supporter (excluded by the active-box filter) and a retracted supporter
    # (excluded by the valid_to query) — neither may inflate the verdict.
    dead = case_box("g44-dead", "1", "test", 0.5).model_copy(
        update={"status": BoxStatus.DEPRECATED}
    )
    assert dead.tier is Tier.CASE
    await _put_box(session, dead)
    dead_fact = await _put_fact(session, dead.id, confidence=1.0)
    await _put_evidence(
        session, source=dead_fact, target=h, box=dead.id, label="SUPPORTS", strength=1.0
    )
    gone = await _put_fact(session, box.id, confidence=1.0)
    await _put_evidence(session, source=gone, target=h, box=box.id, label="SUPPORTS", strength=1.0)
    await _retract_node(session, gone)
    await session.commit()

    result = await QbafAdapter().evaluate(session)
    hs = str(h)
    verdict = next(v for v in result.verdicts if v.id == hs)

    # DF-QuAD: support = prob_sum(0.8, 0.6) = 0.92 (the dead/retracted 1.0 supporters excluded,
    # else it would be higher); attack = 0.3; combine(0.4, 0.92, 0.3) = 0.4 + 0.6·0.62 = 0.772.
    assert verdict.acceptability == pytest.approx(0.772)
    assert verdict.state is HypothesisState.SUPPORTED
    assert verdict.band is AcceptabilityBand.TRUE
    assert result.converged


async def test_persist_writes_acceptability_and_state_without_clobbering_confidence(
    session: AsyncSession,
) -> None:
    await bootstrap_session(session)
    box = case_box("g44-persist", "1", "test", 0.8)
    await _put_box(session, box)
    h = await _put_hypothesis(session, box.id, confidence=0.4)
    f1 = await _put_fact(session, box.id, confidence=1.0)
    await _put_evidence(session, source=f1, target=h, box=box.id, label="SUPPORTS", strength=0.8)
    await session.commit()

    result = await QbafAdapter().evaluate(session)
    persisted = await QbafAdapter().persist_verdicts(session, result.verdicts)
    await session.commit()
    # A SUPPORTED verdict is not a refutation — written, nothing held (V8).
    assert persisted.written >= 1
    assert persisted.held == () and persisted.is_finding is False

    acc, state, conf = await _read_hypothesis(session, h)
    # combine(0.4, 0.8, 0) = 0.4 + 0.6·0.8 = 0.88; state supported; band not stored.
    assert float(acc) == pytest.approx(0.88)
    assert state == HypothesisState.SUPPORTED.value
    # The partial SET preserved the node's pre-existing confidence (not clobbered by a full
    # SET n = {...}) — the whole reason persist uses a targeted SET.
    assert float(conf) == pytest.approx(0.4)


async def test_retracting_a_supporter_lowers_acceptability(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("g44-retract", "1", "test", 0.8)
    await _put_box(session, box)
    h = await _put_hypothesis(session, box.id, confidence=0.3)
    f1 = await _put_fact(session, box.id, confidence=1.0)
    await _put_evidence(session, source=f1, target=h, box=box.id, label="SUPPORTS", strength=0.9)
    await session.commit()
    hs = str(h)

    before = next(v for v in (await QbafAdapter().evaluate(session)).verdicts if v.id == hs)
    assert before.acceptability == pytest.approx(0.3 + 0.7 * 0.9)  # 0.93, supported

    # Retract the sole supporter; its SUPPORTS edge drops out (valid_to query), so the
    # hypothesis falls back toward its intrinsic base score.
    await _retract_node(session, f1)
    await session.commit()
    after = next(v for v in (await QbafAdapter().evaluate(session)).verdicts if v.id == hs)
    assert after.acceptability == pytest.approx(0.3)  # back to base — evidence gone
    assert after.acceptability < before.acceptability


async def _read_hyp_gate_state(session: AsyncSession, hid: uuid.UUID) -> tuple[str, str, str]:
    """Read back (acceptability, state, pending_refutation) of a Hypothesis as raw agtype."""
    rows = await execute_cypher(
        session,
        f"MATCH (h:Hypothesis {{id: '{hid}'}}) "
        "RETURN h.acceptability, h.state, h.pending_refutation",
        returns="acc agtype, state agtype, pending agtype",
    )
    acc, state, pending = rows[0]
    return str(acc), unquote_agtype(state), str(pending)


async def _seed_refuted(session: AsyncSession, name: str) -> uuid.UUID:
    """A hypothesis the QBAF computes a structural ``refuted`` for (net attack, no support)."""
    box = case_box(name, "1", "test", 0.8)
    await _put_box(session, box)
    # Base 0.3 (not 0.5): with the attack it computes REFUTED, and once the attacker is retracted
    # the no-evidence base falls *below* the supported threshold → UNSUPPORTED, not SUPPORTED — the
    # documented no-active-evidence case (cf. test_adjudicate_unsupported_hypothesis).
    h = await _put_hypothesis(session, box.id, confidence=0.3)
    f = await _put_fact(session, box.id, confidence=1.0)
    await _put_evidence(session, source=f, target=h, box=box.id, label="REFUTES", strength=0.9)
    await session.commit()
    return h


async def test_persist_holds_unauthorised_refutation_then_flips_when_authorised(
    session: AsyncSession,
) -> None:
    """V8/§7.2: a structural ``refuted`` is held (state kept, acceptability written,
    ``pending_refutation`` flagged, surfaced as a finding) without an authorising gate decision, and
    flips to ``refuted`` once an authorising :func:`authorise` decision is supplied."""
    await bootstrap_session(session)
    h = await _seed_refuted(session, "v8-hold")
    adapter = QbafAdapter()
    result = await adapter.evaluate(session)
    (v,) = result.verdicts
    assert v.state is HypothesisState.REFUTED  # the engine's structural finding

    # 1) No gate decision → the flip is HELD.
    held = await adapter.persist_verdicts(session, result.verdicts)
    await session.commit()
    assert held.written == 1 and held.is_finding
    (hr,) = held.held
    assert hr.id == str(h) and hr.held_state is HypothesisState.UNSUPPORTED
    assert hr.reason == "ensemble_gate_pending" and hr.decision is None
    acc, state, pending = await _read_hyp_gate_state(session, h)
    assert float(acc) == pytest.approx(v.acceptability)  # acceptability still written
    assert state == "unsupported"  # held at prior (none → unsupported), NOT refuted
    assert pending == "true"

    # 2) An authorising decision (real gate) → the flip persists, pending cleared.
    decision = authorise(
        [affirming(GateChannel.LLM), affirming(GateChannel.SYMBOLIC)], gate=DEFAULT_GATE
    )
    out = await adapter.persist_verdicts(
        session, result.verdicts, gate_decisions={str(h): decision}
    )
    await session.commit()
    assert out.written == 1 and out.held == () and out.is_finding is False
    _, state, pending = await _read_hyp_gate_state(session, h)
    assert state == "refuted"
    assert pending == "false"


async def test_persist_clears_pending_when_a_later_verdict_is_no_longer_refuted(
    session: AsyncSession,
) -> None:
    """A held refutation lifts when the hypothesis is no longer structurally refuted (the attacker
    retracted) — the next persist writes the non-refuted state and clears ``pending_refutation``."""
    await bootstrap_session(session)
    h = await _seed_refuted(session, "v8-clear")
    adapter = QbafAdapter()

    # Hold the unauthorised refutation first.
    refuted = await adapter.evaluate(session)
    await adapter.persist_verdicts(session, refuted.verdicts)
    await session.commit()
    _, _, pending = await _read_hyp_gate_state(session, h)
    assert pending == "true"

    # Retract the attacker → the hypothesis is no longer refuted; persisting clears the flag.
    rows = await execute_cypher(
        session,
        f"MATCH (f)-[:REFUTES]->(:Hypothesis {{id: '{h}'}}) RETURN f.id",
        returns="fid agtype",
    )
    await _retract_node(session, uuid.UUID(unquote_agtype(rows[0][0])))
    await session.commit()

    result2 = await adapter.evaluate(session)
    (v2,) = result2.verdicts
    assert v2.state is HypothesisState.UNSUPPORTED  # base only, no active evidence
    out = await adapter.persist_verdicts(session, result2.verdicts)
    await session.commit()
    assert out.held == () and out.is_finding is False
    _, state, pending = await _read_hyp_gate_state(session, h)
    assert state == "unsupported"
    assert pending == "false"
