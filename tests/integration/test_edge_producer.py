"""G4.3 slice 3 integration test — the edge producer reads real AGE, judges, and writes edges.

Real Postgres+AGE. Seeds an active box with a ``Hypothesis`` and two ``Fact``s that share an
``Actor`` (so the G4.2 structural-entity funnel proposes them as candidates), one of which carries
a full §9.1 credibility chain (``EVIDENCED_BY`` → ``Proposition`` with an ``epistemic_class``, in a
box with a ``reliability_prior``). A scripted LLM classifies one fact as supporting and the other as
refuting; the test then asserts :meth:`EdgeProducer.produce`:

- writes a ``SUPPORTS`` and a ``REFUTES`` edge (evidence → hypothesis) with the three §8/§9
  quantities on them;
- routes **credibility into ``significance``** (the chained fact's significance equals its
  ``effective_credibility``; the un-chained fact's is the identity ``1.0``) while leaving
  **``strength`` the pure connection judgment** (equal to the multi-sample opinion's projected
  probability, *not* discounted by the 0.8 credibility) — the §3.1/§8/§9 separation;
- records one provenance :class:`~iknos.db.orm.Action` (``actor='edge-judge'``);
- produces edges the QBAF adapter (G4.4) actually consumes — :meth:`QbafAdapter.evaluate` moves the
  hypothesis's acceptability off its base score.

The DB is reset before each test (conftest ``_isolate_db``), so these are exact assertions over the
seeded subgraph, not containment.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.credibility import effective_credibility_of
from iknos.core.edge_judge import EdgeJudge, JudgedSign
from iknos.core.edge_producer import EdgeProducer
from iknos.core.qbaf_adapter import QbafAdapter
from iknos.core.subjective_logic import opinion_from_evidence
from iknos.db.age import bootstrap_session, execute_cypher, merge_edge, merge_vertex, unquote_agtype
from iknos.types.edges import EdgeSign
from iknos.types.nodes import Box, Tier

pytestmark = pytest.mark.asyncio

_HYP = "The bearing failed due to metal fatigue"
_SUPPORT = "Metallurgical analysis of the raceway shows fatigue striations"
_SUPPORT2 = "The vibration log shows a rising sub-harmonic over the prior month"
_REFUTE = "The bearing was a new unit installed the day before the failure"


class _ScriptedLLM:
    """Classifies each presented evidence item by its TEXT (so un-permuting recovers the sign)."""

    def __init__(self, by_text: dict[str, JudgedSign], *, model: str = "test-judge") -> None:
        self.model = model
        self._by_text = by_text

    async def guided_complete(self, messages, json_schema, sampling=None) -> dict:  # noqa: ANN001
        block = messages[1]["content"].split("EVIDENCE:\n", 1)[1]
        verdicts = []
        for line in block.splitlines():
            num, _, text_ = line.partition(". ")
            verdicts.append({"ref": int(num), "sign": self._by_text[text_].value})
        return {"verdicts": verdicts}


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_node(
    session: AsyncSession, label: str, box: uuid.UUID, *, statement: str, confidence: float = 0.5
) -> uuid.UUID:
    nid = uuid.uuid4()
    await merge_vertex(
        session,
        label,
        {
            "id": str(nid),
            "box": str(box),
            "tier": str(Tier.CASE),
            "statement": statement,
            "confidence": confidence,
            "valid_to": None,
        },
    )
    return nid


async def _put_actor(session: AsyncSession, box: uuid.UUID) -> uuid.UUID:
    eid = uuid.uuid4()
    await merge_vertex(session, "Actor", {"id": str(eid), "box": str(box), "valid_to": None})
    return eid


async def _involves(session: AsyncSession, *, node: uuid.UUID, entity: uuid.UUID) -> None:
    await merge_edge(
        session, src_id=node, dst_id=entity, label="INVOLVES", props={"role": "subject"}
    )


async def _evidence_proposition(
    session: AsyncSession, *, fact: uuid.UUID, provisional_reasons: list[str] | None = None
) -> None:
    """Give a Fact a §9.1 credibility chain: a Proposition with an epistemic_class it evidences.

    ``provisional_reasons`` (R8) sets the source proposition's §3.1 quarantine reasons (a
    JSON-string list, as ``cypher_map`` writes one) — empty/None for a non-provisional source."""
    pid = uuid.uuid4()
    await merge_vertex(
        session,
        "Proposition",
        {
            "id": str(pid),
            "text": "claim",
            "epistemic_class": "observation",
            "provisional_reasons": provisional_reasons or [],
        },
    )
    await merge_edge(session, src_id=fact, dst_id=pid, label="EVIDENCED_BY", props={})


async def _edges_into(session: AsyncSession, hyp: uuid.UUID, rel: str) -> list[tuple[str, ...]]:
    rows = await execute_cypher(
        session,
        f"MATCH (s)-[r:{rel}]->(t {{id: '{hyp}'}}) "
        "RETURN s.id, r.strength, r.significance, r.sign, r.sign_stable",
        returns="sid agtype, strength agtype, sig agtype, sign agtype, stable agtype",
    )
    return rows


async def test_produce_writes_signed_edges_with_separated_strength_and_significance(
    session: AsyncSession,
) -> None:
    await bootstrap_session(session)
    box = case_box("g43-producer", "1", "test", 0.8)  # reliability_prior 0.8
    await _put_box(session, box)

    actor = await _put_actor(session, box.id)
    h = await _put_node(session, "Hypothesis", box.id, statement=_HYP, confidence=0.5)
    # Two supporters + one refuter (net support, so the QBAF moves off base — symmetric
    # support/attack would cancel back to the base by the balance property, hiding consumption).
    f_support = await _put_node(session, "Fact", box.id, statement=_SUPPORT, confidence=0.9)
    f_support2 = await _put_node(session, "Fact", box.id, statement=_SUPPORT2, confidence=0.9)
    f_refute = await _put_node(session, "Fact", box.id, statement=_REFUTE, confidence=0.9)

    # All three facts share the hypothesis's actor -> the structural funnel proposes them.
    for n in (h, f_support, f_support2, f_refute):
        await _involves(session, node=n, entity=actor)
    # f_support + f_refute carry a (non-provisional) proposition: a credibility chain, and — the V7
    # quarantine gate — the provenance a high-stakes REFUTES needs (an un-evidenced node would be
    # quarantined as missing_provenance). f_support2 is left un-evidenced on purpose: its
    # credibility is undefined (significance 1.0) and, a LOW-stakes corroborator, is not gated.
    await _evidence_proposition(session, fact=f_support)
    await _evidence_proposition(session, fact=f_refute)
    await session.commit()

    judge = EdgeJudge(
        _ScriptedLLM(
            {
                _SUPPORT: JudgedSign.SUPPORTS,
                _SUPPORT2: JudgedSign.SUPPORTS,
                _REFUTE: JudgedSign.REFUTES,
            }
        ),
        n_samples=3,
    )
    result = await EdgeProducer(judge).produce(session)

    # --- the directional edges were written, evidence -> hypothesis ---------------------------
    supports = await _edges_into(session, h, "SUPPORTS")
    refutes = await _edges_into(session, h, "REFUTES")
    assert len(supports) == 2 and len(refutes) == 1
    by_src = {unquote_agtype(row[0]): row for row in supports}
    (s_src, s_strength, s_sig, s_sign, s_stable) = by_src[str(f_support)]
    (_s2_src, _s2_strength, s2_sig, _s2_sign, _s2_stable) = by_src[str(f_support2)]
    (r_src, _r_strength, r_sig, _r_sign, _r_stable) = refutes[0]
    assert unquote_agtype(r_src) == str(f_refute)
    assert unquote_agtype(s_sign) == EdgeSign.SUPPORTS.value  # sign stored alongside the label
    assert str(s_stable) == "true"  # unanimous panel -> stable sign (the §13 finding flag)

    # --- strength is the PURE connection judgment (not credibility-discounted) ----------------
    # 3/3 supports -> opinion_from_evidence(3, 0); reliability is identity, so strength is exactly
    # that opinion's projected probability — the credibility 0.8 does NOT enter strength (§3.1/§8).
    expected_strength = opinion_from_evidence(3, 0).projected_probability
    assert float(str(s_strength)) == pytest.approx(expected_strength)

    # --- credibility is routed into SIGNIFICANCE (§9) -----------------------------------------
    cred = await effective_credibility_of(session, f_support)
    assert cred is not None
    # Uniform tier weight (MVP) -> significance == effective_credibility for the chained nodes
    # (f_support + f_refute share the box, so the refuter's significance is the same credibility).
    assert float(str(s_sig)) == pytest.approx(cred)
    assert float(str(r_sig)) == pytest.approx(cred)
    assert cred < 1.0  # the 0.8 reliability_prior really did flow through
    # The un-evidenced supporter has no credibility chain -> significance is identity 1.0
    # (undefined, not zero); a LOW-stakes corroborator, so V7 does not quarantine it.
    assert float(str(s2_sig)) == pytest.approx(1.0)

    # --- provenance: exactly one Action for the one judged hypothesis -------------------------
    assert len(result.action_ids) == 1
    count = await session.execute(
        text("SELECT count(*) FROM actions WHERE actor = 'edge-judge' AND action_type = 'judge'")
    )
    assert count.scalar_one() == 1

    # --- the produced edges are exactly what the QBAF adapter (G4.4) consumes -----------------
    adj = await QbafAdapter().evaluate(session)
    verdict = next(v for v in adj.verdicts if v.id == str(h))
    # Net support (2 vs 1) raises acceptability above the base (0.5) — the engine consumed the
    # produced edges. (Symmetric support/attack would have cancelled back to base.)
    assert verdict.acceptability > 0.5

    # --- the result object mirrors the graph --------------------------------------------------
    assert {(e.evidence, e.sign) for e in result.edges} == {
        (str(f_support), EdgeSign.SUPPORTS),
        (str(f_support2), EdgeSign.SUPPORTS),
        (str(f_refute), EdgeSign.REFUTES),
    }
    assert result.dropped == ()
    assert result.quarantined == ()  # every source is non-provisional + evidenced (V7)
    assert result.is_finding is False


async def test_corroborate_scopes_to_one_hypothesis_and_records_envelope_action(
    session: AsyncSession,
) -> None:
    """G4.5 §12 entry point: ``corroborate(h)`` gathers only *h*'s evidence and records its own
    Action wrapping the edge-judge Action — a second hypothesis in the same box is untouched."""
    await bootstrap_session(session)
    box = case_box("g45-corroborate", "1", "test", 0.8)
    await _put_box(session, box)

    # Two hypotheses, each with its **own** actor so the structural funnel scopes candidates per
    # hypothesis: corroborate(h) must judge h's facts only, never h2's.
    actor_h = await _put_actor(session, box.id)
    actor_h2 = await _put_actor(session, box.id)
    h = await _put_node(session, "Hypothesis", box.id, statement=_HYP, confidence=0.5)
    h2 = await _put_node(
        session, "Hypothesis", box.id, statement="An unrelated hypothesis", confidence=0.5
    )
    f_support = await _put_node(session, "Fact", box.id, statement=_SUPPORT, confidence=0.9)
    f_refute = await _put_node(session, "Fact", box.id, statement=_REFUTE, confidence=0.9)
    f_other = await _put_node(session, "Fact", box.id, statement=_SUPPORT2, confidence=0.9)

    for n in (h, f_support, f_refute):
        await _involves(session, node=n, entity=actor_h)
    for n in (h2, f_other):
        await _involves(session, node=n, entity=actor_h2)
    # Provenance for the high-stakes REFUTES (V7) + credibility chains.
    await _evidence_proposition(session, fact=f_support)
    await _evidence_proposition(session, fact=f_refute)
    await _evidence_proposition(session, fact=f_other)
    await session.commit()

    judge = EdgeJudge(
        _ScriptedLLM(
            {
                _SUPPORT: JudgedSign.SUPPORTS,
                _REFUTE: JudgedSign.REFUTES,
                _SUPPORT2: JudgedSign.SUPPORTS,
            }
        ),
        n_samples=3,
    )
    res = await EdgeProducer(judge).corroborate(session, h)

    # Scoped to h: supporters/refuters split, and h2 gets nothing.
    assert {e.evidence for e in res.supporters} == {str(f_support)}
    assert {e.evidence for e in res.refuters} == {str(f_refute)}
    assert len(await _edges_into(session, h, "SUPPORTS")) == 1
    assert len(await _edges_into(session, h, "REFUTES")) == 1
    assert await _edges_into(session, h2, "SUPPORTS") == []  # the other hypothesis untouched

    # The corroborate envelope Action names h, splits the evidence, and references the edge-judge
    # Action it drove — without replacing it (edge provenance stays under actor='edge-judge').
    co = await session.execute(
        text("SELECT inputs, outputs FROM actions WHERE actor='corroborate'")
    )
    inputs, outputs = co.one()
    assert inputs["hypothesis"] == str(h)
    assert set(outputs["supporters"]) == {str(f_support)}
    assert set(outputs["refuters"]) == {str(f_refute)}
    assert len(outputs["edge_actions"]) == 1
    ej = await session.execute(
        text("SELECT count(*) FROM actions WHERE actor='edge-judge' AND inputs->>'hypothesis'=:h"),
        {"h": str(h)},
    )
    assert ej.scalar_one() == 1
    assert res.action_id is not None and res.is_finding is False


async def test_corroborate_with_no_candidates_still_records_an_action(
    session: AsyncSession,
) -> None:
    """A hypothesis with no candidate evidence is an auditable "looked, gathered nothing" — the
    operator still emits its Action (never an invisible run), with empty evidence lists."""
    await bootstrap_session(session)
    box = case_box("g45-empty", "1", "test", 0.8)
    await _put_box(session, box)
    h = await _put_node(session, "Hypothesis", box.id, statement=_HYP, confidence=0.5)
    await session.commit()

    judge = EdgeJudge(_ScriptedLLM({}), n_samples=3)
    res = await EdgeProducer(judge).corroborate(session, h)

    assert res.supporters == () and res.refuters == ()
    assert res.production.edges == ()
    co = await session.execute(
        text("SELECT outputs FROM actions WHERE actor='corroborate' AND inputs->>'hypothesis'=:h"),
        {"h": str(h)},
    )
    outputs = co.scalar_one()
    assert outputs["supporters"] == [] and outputs["refuters"] == []
    assert outputs["edge_actions"] == []


async def test_produce_quarantines_a_provisional_sourced_refutes(session: AsyncSession) -> None:
    """V7 / §3.1, end to end on real AGE: a Fact whose source Proposition is provisional drives a
    REFUTES; the gate drops the edge from the plan (no REFUTES persisted) and records it on the
    producing Action's outputs.quarantined — a triage signal, never a silent skip."""
    await bootstrap_session(session)
    box = case_box("v7-quarantine", "1", "test", 0.8)
    await _put_box(session, box)

    actor = await _put_actor(session, box.id)
    h = await _put_node(session, "Hypothesis", box.id, statement=_HYP, confidence=0.5)
    f_refute = await _put_node(session, "Fact", box.id, statement=_REFUTE, confidence=0.9)
    for n in (h, f_refute):
        await _involves(session, node=n, entity=actor)
    # The refuter's source proposition is provisional -> its REFUTES is a quarantined move.
    await _evidence_proposition(session, fact=f_refute, provisional_reasons=["low_faithfulness"])
    await session.commit()

    judge = EdgeJudge(_ScriptedLLM({_REFUTE: JudgedSign.REFUTES}), n_samples=3)
    result = await EdgeProducer(judge).produce(session)

    # No REFUTES edge persisted.
    assert await _edges_into(session, h, "REFUTES") == []
    assert result.edges == ()
    # The drop is surfaced on the result and recorded on the producing Action.
    (q,) = result.quarantined
    assert q.evidence == str(f_refute) and q.sign is EdgeSign.REFUTES
    assert q.reasons == ("low_faithfulness",)
    act = await session.execute(
        text(
            "SELECT outputs FROM actions WHERE actor = 'edge-judge' "
            "AND inputs->>'hypothesis' = :hid"
        ),
        {"hid": str(h)},
    )
    quarantined = act.one().outputs["quarantined"]
    assert quarantined == [
        {
            "evidence": str(f_refute),
            "sign": "refutes",
            "reasons": ["low_faithfulness"],
            "stakes": "high",
        }
    ]

    # The QBAF adapter never sees the dropped attack: the hypothesis stays at its base.
    adj = await QbafAdapter().evaluate(session)
    verdict = next(v for v in adj.verdicts if v.id == str(h))
    assert verdict.acceptability == pytest.approx(0.5)
