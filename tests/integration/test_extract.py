"""Phase 2 G2.2 integration test — the ``extract`` operator end to end.

Exercises real Postgres+AGE persistence with the LLM mocked (no vLLM needed). A
Proposition + its Span are created by the test (Phase 1 already materializes them);
the operator turns the Proposition into a Fact with its Actor/Object nodes, role-tagged
``INVOLVES`` edges, ``EVIDENCED_BY`` provenance, the two annotations, and an Action.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import case_box
from iknos.core.extract import ExtractInput, Extractor
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.db.spans import resolve_span_text
from iknos.types.nodes import Proposition, Span

pytestmark = pytest.mark.asyncio


def _extractor(llm_return: dict) -> Extractor:
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(return_value=llm_return)
    return Extractor(llm, concurrency=4)


async def _seed_proposition(
    session: AsyncSession, raw: str, *, text_: str, faithfulness: float | None = None
) -> tuple[Span, Proposition]:
    """Create a Document + Span + Proposition (EVIDENCED_BY the Span) the operator reads."""
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": raw},
    )
    await execute_cypher(session, f"CREATE (:Document {cypher_map({'id': str(doc_id)})})")
    span = Span(id=uuid.uuid4(), document_id=doc_id, start=0, end=len(raw))
    await execute_cypher(
        session,
        "CREATE (:Span "
        + cypher_map(
            {"id": str(span.id), "document_id": str(doc_id), "start": span.start, "end": span.end}
        )
        + ")",
    )
    prop = Proposition(id=uuid.uuid4(), text=text_, faithfulness=faithfulness)
    await execute_cypher(
        session,
        "CREATE (:Proposition "
        + cypher_map({"id": str(prop.id), "text": prop.text, "faithfulness": prop.faithfulness})
        + ")",
    )
    await execute_cypher(
        session,
        f"MATCH (p:Proposition {cypher_map({'id': str(prop.id)})}), "
        f"(s:Span {cypher_map({'id': str(span.id)})}) CREATE (p)-[:EVIDENCED_BY]->(s)",
    )
    await session.commit()
    return span, prop


async def test_extract_operator_end_to_end(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "The operator restarted the pump."
    span, prop = await _seed_proposition(
        session, raw, text_="The operator restarted the pump.", faithfulness=0.9
    )
    box = case_box("case-doc", "1", "test", 0.8)

    ex = _extractor(
        {
            "entities": [
                {"label": "operator", "type": "person", "kind": "actor", "role": "subject"},
                {"label": "pump", "type": "equipment", "kind": "object", "role": "object"},
            ]
        }
    )
    action_id = await ex.extract_proposition(
        session, ExtractInput(proposition=prop, span_ids=[span.id]), box
    )
    assert action_id is not None

    # --- Fact vertex: boxed, tiered (from the case box), both annotations, statement ---
    # All graph assertions scope to this test's unique box id: the integration graph is
    # shared across tests with no per-test cleanup, so an unscoped MATCH would see others'.
    box_match = cypher_map({"box": str(box.id)})
    rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {box_match}) "
        "RETURN f.box, f.tier, f.statement, f.support_count, f.confidence",
        returns="box agtype, tier agtype, stmt agtype, sc agtype, conf agtype",
    )
    assert len(rows) == 1
    fbox, ftier, fstmt, fsc, fconf = rows[0]
    assert str(fbox).strip('"') == str(box.id)
    assert str(ftier).strip('"') == "case"  # resolved from the case box (§9)
    assert str(fstmt).strip('"') == "The operator restarted the pump."
    assert int(str(fsc)) == 1  # base fact: one EVIDENCED_BY grounding (§12 Layer A)
    assert float(str(fconf)) == pytest.approx(0.9)  # seeded from proposition faithfulness

    # --- Actor / Object vertices, fresh nodes, box-tagged, with role-tagged INVOLVES ---
    # Matched by explicit label (the codebase convention) rather than labels(): the actor
    # entity lands on :Actor with role subject, the object on :Object with role object.
    actor = await execute_cypher(
        session,
        f"MATCH (f:Fact {box_match})-[r:INVOLVES]->(a:Actor) RETURN a.label, a.type, a.box, r.role",
        returns="lab agtype, typ agtype, box agtype, role agtype",
    )
    assert len(actor) == 1
    assert str(actor[0][0]).strip('"') == "operator"
    assert str(actor[0][1]).strip('"') == "person"
    assert str(actor[0][2]).strip('"') == str(box.id)
    assert str(actor[0][3]).strip('"') == "subject"

    obj = await execute_cypher(
        session,
        f"MATCH (f:Fact {box_match})-[r:INVOLVES]->(o:Object) RETURN o.label, o.box, r.role",
        returns="lab agtype, box agtype, role agtype",
    )
    assert len(obj) == 1
    assert str(obj[0][0]).strip('"') == "pump"
    assert str(obj[0][1]).strip('"') == str(box.id)
    assert str(obj[0][2]).strip('"') == "object"

    # --- Provenance (§10.2): Fact -EVIDENCED_BY-> Proposition AND -> Span -> source text ---
    ev_prop = await execute_cypher(
        session,
        f"MATCH (f:Fact)-[:EVIDENCED_BY]->(p:Proposition {cypher_map({'id': str(prop.id)})}) "
        "RETURN f",
        returns="f agtype",
    )
    assert len(ev_prop) == 1
    ev_span = await execute_cypher(
        session,
        f"MATCH (f:Fact)-[:EVIDENCED_BY]->(s:Span {cypher_map({'id': str(span.id)})}) RETURN f",
        returns="f agtype",
    )
    assert len(ev_span) == 1
    assert await resolve_span_text(session, span.document_id, span.start, span.end) == raw

    # --- Action (§10.1): actor=extractor, joinable to the Fact + entities by output id ---
    act = await session.execute(
        text(
            "SELECT action_type, model, inputs, outputs FROM actions "
            "WHERE actor = 'extractor' AND inputs->>'proposition' = :pid"
        ),
        {"pid": str(prop.id)},
    )
    rec = act.one()
    assert rec.action_type == "extract"
    assert rec.model == "test-model"
    assert str(span.id) in rec.inputs["spans"]
    assert len(rec.outputs["actors"]) == 1
    assert len(rec.outputs["objects"]) == 1
    assert len(rec.outputs["evidenced_by"]) == 2  # proposition + one span

    # --- Idempotency: a second run is a no-op (Action-based skip), no new Fact, no LLM call ---
    calls_before = ex.llm.guided_complete.await_count
    again = await ex.extract_proposition(
        session, ExtractInput(proposition=prop, span_ids=[span.id]), box
    )
    assert again is None
    assert ex.llm.guided_complete.await_count == calls_before
    count = await execute_cypher(
        session, f"MATCH (f:Fact {box_match}) RETURN count(f)", returns="n agtype"
    )
    assert int(str(count[0][0])) == 1


async def test_extract_no_entities_still_creates_evidenced_fact(session: AsyncSession) -> None:
    """A statement with no concrete entity yields a Fact with provenance but no INVOLVES."""
    await bootstrap_session(session)
    raw = "It was, on the whole, regrettable."
    span, prop = await _seed_proposition(session, raw, text_="The situation was regrettable.")
    box = case_box("case-empty", "1", "test", 0.8)

    ex = _extractor({"entities": []})
    await ex.extract_proposition(session, ExtractInput(proposition=prop, span_ids=[span.id]), box)

    box_match = cypher_map({"box": str(box.id)})
    fact = await execute_cypher(
        session, f"MATCH (f:Fact {box_match}) RETURN f.confidence", returns="c agtype"
    )
    assert len(fact) == 1
    # No verifier ran upstream → faithfulness null → confidence seeded to the Viterbi identity.
    assert float(str(fact[0][0])) == pytest.approx(1.0)
    involves = await execute_cypher(
        session, f"MATCH (:Fact {box_match})-[r:INVOLVES]->() RETURN count(r)", returns="n agtype"
    )
    assert int(str(involves[0][0])) == 0
    ev = await execute_cypher(
        session,
        f"MATCH (:Fact {box_match})-[r:EVIDENCED_BY]->() RETURN count(r)",
        returns="n agtype",
    )
    assert int(str(ev[0][0])) == 2  # proposition + span


async def test_extract_batch_skips_already_extracted(session: AsyncSession) -> None:
    """The batch driver extracts only the pending propositions and is idempotent per item."""
    await bootstrap_session(session)
    raw = "The bearing failed."
    span_a, prop_a = await _seed_proposition(session, raw, text_="The bearing failed.")
    span_b, prop_b = await _seed_proposition(session, raw, text_="The shaft cracked.")
    box = case_box("case-batch", "1", "test", 0.8)

    ex = _extractor({"entities": [{"label": "bearing", "kind": "object", "role": "subject"}]})
    first = await ex.extract_propositions(
        session,
        [
            ExtractInput(proposition=prop_a, span_ids=[span_a.id]),
            ExtractInput(proposition=prop_b, span_ids=[span_b.id]),
        ],
        box,
    )
    assert len(first) == 2

    # Re-run with prop_a already done + a brand-new prop_c: only prop_c is extracted.
    span_c, prop_c = await _seed_proposition(session, raw, text_="The seal leaked.")
    second = await ex.extract_propositions(
        session,
        [
            ExtractInput(proposition=prop_a, span_ids=[span_a.id]),
            ExtractInput(proposition=prop_c, span_ids=[span_c.id]),
        ],
        box,
    )
    assert len(second) == 1  # prop_a skipped, prop_c extracted
    total = await execute_cypher(
        session,
        f"MATCH (f:Fact {cypher_map({'box': str(box.id)})}) RETURN count(f)",
        returns="n agtype",
    )
    assert int(str(total[0][0])) == 3
