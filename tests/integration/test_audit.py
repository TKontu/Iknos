"""Phase 2 G2.7 integration test — the §10.2 audit reach-back end to end.

Exercises real Postgres+AGE: extract a Fact (LLM mocked), then prove the auditability
guarantee from the graph — from the Fact reach its Proposition, its Span + source text, and
the producing Action — and that the box-level invariant (`audit_box_facts`) holds for a
clean extraction and flags a deliberately un-provenanced Fact.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import case_box
from iknos.core.extract import ExtractInput, Extractor
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.provenance.audit import (
    MISSING_ACTION,
    MISSING_SOURCE_TEXT,
    MISSING_SPAN,
    audit_box_facts,
    fact_provenance,
    producing_action,
)
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


async def _fact_id_in_box(session: AsyncSession, box_id: uuid.UUID) -> uuid.UUID:
    rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {cypher_map({'box': str(box_id)})}) RETURN f.id",
        returns="fid agtype",
    )
    assert len(rows) == 1
    return uuid.UUID(str(rows[0][0]).strip('"'))


async def test_fact_provenance_reaches_span_text_and_action(session: AsyncSession) -> None:
    await bootstrap_session(session)
    raw = "The operator restarted the pump."
    span, prop = await _seed_proposition(session, raw, text_=raw, faithfulness=0.9)
    box = case_box("audit-doc", "1", "test", 0.8)

    ex = _extractor(
        {"entities": [{"label": "pump", "type": "equipment", "kind": "object", "role": "object"}]}
    )
    await ex.extract_proposition(session, ExtractInput(proposition=prop, span_ids=[span.id]), box)

    fact_id = await _fact_id_in_box(session, box.id)
    prov = await fact_provenance(session, fact_id)
    assert prov is not None

    # Proposition reachable (§10.2).
    assert prov.proposition_id == prop.id

    # Span + resolved source text reachable.
    assert len(prov.spans) == 1
    sref = prov.spans[0]
    assert sref.span_id == span.id
    assert sref.document_id == span.document_id
    assert sref.text == raw

    # Producing Action reachable and identified (§10.1).
    assert prov.action is not None
    assert prov.action.action_type == "extract"
    assert prov.action.actor == "extractor"
    assert prov.action.model == "test-model"

    assert prov.is_auditable is True
    # producing_action agrees with the bundled reach-back.
    pa = await producing_action(session, fact_id)
    assert pa is not None and pa.id == prov.action.id

    # Box invariant: a cleanly-extracted box has zero auditability violations.
    assert await audit_box_facts(session, box.id) == []


async def test_fact_provenance_none_for_unknown_fact(session: AsyncSession) -> None:
    await bootstrap_session(session)
    assert await fact_provenance(session, uuid.uuid4()) is None


async def test_audit_flags_unprovenanced_fact(session: AsyncSession) -> None:
    """A Fact created with neither evidence edges nor a producing Action is flagged with the
    full gap set — the negative case the exit-criterion check must catch."""
    await bootstrap_session(session)
    box = case_box("audit-orphan", "1", "test", 0.8)
    orphan = uuid.uuid4()
    await execute_cypher(
        session,
        "CREATE (:Fact " + cypher_map({"id": str(orphan), "box": str(box.id)}) + ")",
    )
    await session.commit()

    violations = await audit_box_facts(session, box.id)
    assert len(violations) == 1
    assert violations[0].fact_id == orphan
    assert violations[0].gaps == frozenset({MISSING_SPAN, MISSING_ACTION})


async def test_audit_flags_missing_source_text(session: AsyncSession) -> None:
    """A Fact whose Span exists but whose document_content row is gone reaches the span node
    yet not the source text — flagged MISSING_SOURCE_TEXT (and MISSING_ACTION: hand-built,
    no extract Action)."""
    await bootstrap_session(session)
    box = case_box("audit-notext", "1", "test", 0.8)
    fact_id = uuid.uuid4()
    span_id = uuid.uuid4()
    missing_doc = uuid.uuid4()  # no document_content row for this id
    await execute_cypher(
        session, "CREATE (:Fact " + cypher_map({"id": str(fact_id), "box": str(box.id)}) + ")"
    )
    await execute_cypher(
        session,
        "CREATE (:Span "
        + cypher_map({"id": str(span_id), "document_id": str(missing_doc), "start": 0, "end": 5})
        + ")",
    )
    await execute_cypher(
        session,
        f"MATCH (f:Fact {cypher_map({'id': str(fact_id)})}), "
        f"(s:Span {cypher_map({'id': str(span_id)})}) CREATE (f)-[:EVIDENCED_BY]->(s)",
    )
    await session.commit()

    prov = await fact_provenance(session, fact_id)
    assert prov is not None
    assert len(prov.spans) == 1 and prov.spans[0].text is None
    assert prov.is_auditable is False

    violations = await audit_box_facts(session, box.id)
    assert len(violations) == 1
    assert violations[0].gaps == frozenset({MISSING_SOURCE_TEXT, MISSING_ACTION})
