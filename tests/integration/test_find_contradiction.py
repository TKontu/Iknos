"""G4.5 integration test — the ``find-contradiction`` operator end-to-end on real AGE + pgvector.

Real Postgres+AGE+pgvector. The §12 operator composes the targeted refuter pass (corroborate) → the
§7.2 ensemble gate → the §12 revision loop, and the two paths that matter are pinned against the
live store:

- **authorise → retract.** A hypothesis ``h`` and a refuter ``r`` whose claim is the **embedding
  twin** of ``h``'s, asserted with the *opposite* :class:`~iknos.types.epistemic.Polarity` — a real
  ``P ∧ ¬P``. The blind panel judges ``r`` a stable ``REFUTES`` (LLM AFFIRM) and the **real clingo**
  symbolic sub-region the operator builds confirms the contradiction (SYMBOLIC AFFIRM), so
  ``DEFAULT_GATE`` authorises the flip and ``stabilize`` retracts ``h``. The symbolic channel is the
  genuine one (``core/symbolic_gate``), not mocked — gotcha #2 of the design: the authorise path
  only fires on a constructed real contradiction.
- **held (the safe default).** Same shape but ``r``'s claim is a *different*, embedding-dissimilar
  proposition: the panel still says ``REFUTES`` (LLM AFFIRM) but the symbolic check finds the two
  share no claim atom → ABSTAIN, so ``DEFAULT_GATE`` (which *requires* SYMBOLIC) **withholds**. The
  V8 ``persist_verdicts`` filter holds ``h`` at its prior state + ``pending_refutation`` — the §13
  finding, surfaced never written. Nothing is retracted.

The DB is reset before each test (conftest ``_isolate_db``), so these are exact assertions over the
seeded sub-region.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.edge_judge import EdgeJudge, JudgedSign
from iknos.core.edge_producer import EdgeProducer
from iknos.core.find_contradiction import FindContradiction
from iknos.db.age import bootstrap_session, execute_cypher, merge_edge, merge_vertex, unquote_agtype
from iknos.db.orm import PropositionEmbedding
from iknos.types.epistemic import Polarity
from iknos.types.nodes import Box, Tier

pytestmark = pytest.mark.asyncio

_MODEL = "fc-knn-model"  # the shared vector-space identity (G1.16) for the in-space embeddings

_HYP = "The bearing failed due to metal fatigue"
_REFUTE_TWIN = "The bearing did not fail due to metal fatigue"  # the polarity twin (negated _HYP)
_REFUTE_OTHER = (
    "The bearing was a new unit installed the day before the failure"  # contrary, not ¬h
)


def _vec(*nonzero: tuple[int, float]) -> list[float]:
    """A 1024-d vector with the given ``(index, value)`` components set, rest zero."""
    v = [0.0] * 1024
    for i, x in nonzero:
        v[i] = x
    return v


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
    session: AsyncSession, label: str, box: uuid.UUID, *, statement: str, confidence: float
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


async def _embed_claim(
    session: AsyncSession,
    *,
    node: uuid.UUID,
    document_id: uuid.UUID,
    text_: str,
    polarity: Polarity,
    vector: list[float],
) -> None:
    """Give a reasoning node a claim with polarity + a dense vector (the symbolic sub-region input):
    node -EVIDENCED_BY-> Proposition (non-provisional, so a high-stakes REFUTES is not quarantined),
    plus its ``proposition_embeddings`` row in the shared model space."""
    pid = uuid.uuid4()
    await merge_vertex(
        session,
        "Proposition",
        {
            "id": str(pid),
            "text": text_,
            "polarity": str(polarity),
            "epistemic_class": "observation",
            "provisional_reasons": [],
        },
    )
    await merge_edge(session, src_id=node, dst_id=pid, label="EVIDENCED_BY", props={})
    session.add(
        PropositionEmbedding(
            proposition_id=pid, document_id=document_id, embedding=vector, model=_MODEL
        )
    )


async def _doc(session: AsyncSession) -> uuid.UUID:
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": "src"},
    )
    return doc_id


async def _read(session: AsyncSession, nid: uuid.UUID) -> dict[str, str]:
    rows = await execute_cypher(
        session,
        f"MATCH (n {{id: '{nid}'}}) RETURN n.state, n.valid_to, n.pending_refutation",
        returns="state agtype, valid_to agtype, pending agtype",
    )
    state, valid_to, pending = rows[0]

    def _norm(v: object) -> str:
        return "null" if v is None or str(v) in ("null", "None") else unquote_agtype(v)

    return {"state": _norm(state), "valid_to": _norm(valid_to), "pending": _norm(pending)}


async def _seed(
    session: AsyncSession, name: str, *, refute_text: str, refute_polarity: Polarity, twin: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    """h (asserted claim) + r (a REFUTES candidate sharing h's actor). ``twin`` controls whether r's
    claim embeds onto h's (a real ¬h) or is dissimilar (merely contrary)."""
    box = case_box(name, "1", "test", 0.8)
    await _put_box(session, box)
    doc_id = await _doc(session)
    actor = await _put_actor(session, box.id)

    h = await _put_node(session, "Hypothesis", box.id, statement=_HYP, confidence=0.3)
    r = await _put_node(session, "Fact", box.id, statement=refute_text, confidence=0.9)
    # Shared actor → the structural funnel proposes (r, h) regardless of embedding distance.
    await _involves(session, node=h, entity=actor)
    await _involves(session, node=r, entity=actor)

    h_vec = _vec((0, 1.0))
    r_vec = _vec((0, 1.0)) if twin else _vec((1, 1.0))  # twin: same claim space; else orthogonal
    await _embed_claim(
        session, node=h, document_id=doc_id, text_=_HYP, polarity=Polarity.ASSERTED, vector=h_vec
    )
    await _embed_claim(
        session,
        node=r,
        document_id=doc_id,
        text_=refute_text,
        polarity=refute_polarity,
        vector=r_vec,
    )
    await session.commit()
    return h, r


def _operator() -> FindContradiction:
    judge = EdgeJudge(
        _ScriptedLLM(
            {
                _HYP: JudgedSign.SUPPORTS,
                _REFUTE_TWIN: JudgedSign.REFUTES,
                _REFUTE_OTHER: JudgedSign.REFUTES,
            }
        ),
        n_samples=3,
    )
    return FindContradiction(EdgeProducer(judge))


async def test_real_symbolic_contradiction_authorises_and_retracts(session: AsyncSession) -> None:
    await bootstrap_session(session)
    h, r = await _seed(
        session,
        "fc-authorise",
        refute_text=_REFUTE_TWIN,
        refute_polarity=Polarity.NEGATED,
        twin=True,
    )

    res = await _operator().run(session, h)

    # The panel surfaced a stable refuter and the clingo sub-region confirmed a real P ∧ ¬P → the
    # gate authorised and stabilize retracted h (the §12 REFUTES → retract feedback).
    assert {e.evidence for e in res.corroboration.refuters} == {str(r)}
    assert res.authorised is True
    assert res.retracted == frozenset({str(h)})

    hr = await _read(session, h)
    assert hr["valid_to"] != "null"  # h retracted (authorised flip)

    # The operator envelope records the gate decision + the loop it drove (full provenance, §10.1).
    row = await session.execute(
        text("SELECT outputs FROM actions WHERE actor='find-contradiction'")
    )
    outputs = row.scalar_one()
    assert outputs["authorised"] is True
    assert outputs["gate"]["outcome"] == "authorised"
    assert outputs["retracted"] == [str(h)]
    assert outputs["corroborate_action"]  # the wrapped corroborate envelope is referenced
    assert outputs["loop_actions"]


async def test_no_symbolic_contradiction_holds_the_flip_as_pending(session: AsyncSession) -> None:
    await bootstrap_session(session)
    h, r = await _seed(
        session,
        "fc-hold",
        refute_text=_REFUTE_OTHER,
        refute_polarity=Polarity.ASSERTED,
        twin=False,
    )

    res = await _operator().run(session, h)

    # LLM affirmed (stable REFUTES) but the symbolic check abstains (no shared claim atom), so
    # DEFAULT_GATE — which requires SYMBOLIC — withholds: the flip is held, not retracted.
    assert {e.evidence for e in res.corroboration.refuters} == {str(r)}
    assert res.authorised is False
    assert res.is_finding is True  # the held refutation is the §13 surface
    assert res.retracted == frozenset()

    hr = await _read(session, h)
    assert hr["valid_to"] == "null"  # h stands (no retraction)
    assert hr["state"] != "refuted"  # the flip was withheld
    assert hr["pending"] == "true"  # surfaced as pending_refutation (V8)

    row = await session.execute(
        text("SELECT outputs FROM actions WHERE actor='find-contradiction'")
    )
    outputs = row.scalar_one()
    assert outputs["authorised"] is False
    assert outputs["gate"]["outcome"] == "withheld"
    # The withholding reason names the unmet required symbolic channel.
    assert any("symbolic" in reason for reason in outputs["gate"]["reasons"])
