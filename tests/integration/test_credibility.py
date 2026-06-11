"""Phase 2 G2.6 integration test — sensitivity seeding + the use-time credibility read.

Real Postgres+AGE with the extractor's LLM mocked. The extractor seeds a Fact from a
proposition; G2.6 (1) seeds the Fact's sensitivity as the lub of its source Span(s) (§9.1)
and (2) leaves credibility *derived* — ``effective_credibility_of`` computes it at use-time
from the stored inputs (box ``reliability_prior`` × the epistemic-class-gated interest
modifier), never a stored scalar.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.registry import create_box
from iknos.boxes.serde import case_box
from iknos.core.credibility import effective_credibility_of
from iknos.core.extract import ExtractInput, Extractor
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.types.epistemic import EpistemicClass
from iknos.types.governance import InterestAlignment, Sensitivity, SensitivityLevel
from iknos.types.nodes import Proposition, Span

pytestmark = pytest.mark.asyncio


def _extractor(entities: list[dict]) -> Extractor:
    llm = MagicMock()
    llm.model = "test-model"
    llm.guided_complete = AsyncMock(return_value={"entities": entities})
    return Extractor(llm, concurrency=2)


async def _seed_proposition(
    session: AsyncSession,
    *,
    text_: str,
    epistemic_class: EpistemicClass,
    span_sensitivity: Sensitivity,
) -> tuple[Span, Proposition]:
    """Create a Document + Span (carrying ``span_sensitivity``) + Proposition to extract."""
    doc_id = uuid.uuid4()
    await execute_cypher(session, f"CREATE (:Document {cypher_map({'id': str(doc_id)})})")
    span = Span(id=uuid.uuid4(), document_id=doc_id, start=0, end=len(text_))
    span_props = {
        "id": str(span.id),
        "document_id": str(doc_id),
        "start": span.start,
        "end": span.end,
        **span_sensitivity.flatten(),
    }
    await execute_cypher(session, f"CREATE (:Span {cypher_map(span_props)})")
    prop = Proposition(id=uuid.uuid4(), text=text_, epistemic_class=epistemic_class)
    await execute_cypher(
        session,
        "CREATE (:Proposition "
        + cypher_map(
            {"id": str(prop.id), "text": prop.text, "epistemic_class": prop.epistemic_class}
        )
        + ")",
    )
    await session.commit()
    return span, prop


async def _fact_id_for(session: AsyncSession, prop: Proposition, box) -> uuid.UUID:
    bx = cypher_map({"box": str(box.id)})
    rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {bx})-[:EVIDENCED_BY]->(p:Proposition {cypher_map({'id': str(prop.id)})}) "
        "RETURN f.id",
        returns="fid agtype",
    )
    return uuid.UUID(str(rows[0][0]).strip('"'))


async def test_fact_inherits_span_sensitivity(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("cred-sensitivity", "1", "test", 0.8)
    await create_box(session, box)
    span, prop = await _seed_proposition(
        session,
        text_="The bearing failed.",
        epistemic_class=EpistemicClass.OBSERVATION,
        span_sensitivity=Sensitivity(
            level=SensitivityLevel.CONFIDENTIAL, compartments=frozenset({"eu"})
        ),
    )
    ex = _extractor(
        [{"label": "bearing", "type": "component", "kind": "object", "role": "subject"}]
    )
    await ex.extract_proposition(session, ExtractInput(proposition=prop, span_ids=[span.id]), box)

    # The Fact's sensitivity is the lub of its source span (§9.1), not the lattice origin.
    fid = await _fact_id_for(session, prop, box)
    rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {cypher_map({'id': str(fid)})}) "
        "RETURN f.sensitivity_level, f.sensitivity_compartments",
        returns="lvl agtype, comp agtype",
    )
    level, comp = rows[0]
    assert str(level).strip('"') == "confidential"
    assert "eu" in str(comp)


async def test_effective_credibility_observation_is_box_reliability(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("cred-observation", "1", "test", 0.7)
    await create_box(session, box)
    span, prop = await _seed_proposition(
        session,
        text_="The rolling surface shows particle indentations.",
        epistemic_class=EpistemicClass.OBSERVATION,
        span_sensitivity=Sensitivity(),
    )
    ex = _extractor(
        [{"label": "surface", "type": "component", "kind": "object", "role": "subject"}]
    )
    await ex.extract_proposition(session, ExtractInput(proposition=prop, span_ids=[span.id]), box)

    fid = await _fact_id_for(session, prop, box)
    # Observation: credibility is interest-independent -> the box reliability (0.7), unjudged.
    cred = await effective_credibility_of(session, fid)
    assert cred == pytest.approx(0.7)


async def test_effective_credibility_judgement_discounts_self_serving(
    session: AsyncSession,
) -> None:
    await bootstrap_session(session)
    box = case_box("cred-judgement", "1", "test", 0.8)
    await create_box(session, box)
    span, prop = await _seed_proposition(
        session,
        text_="Therefore it was an assembly fault.",
        epistemic_class=EpistemicClass.JUDGEMENT,
        span_sensitivity=Sensitivity(),
    )
    ex = _extractor([{"label": "assembly", "type": "process", "kind": "object", "role": "subject"}])
    await ex.extract_proposition(session, ExtractInput(proposition=prop, span_ids=[span.id]), box)
    fid = await _fact_id_for(session, prop, box)

    # Unjudged judgement -> box reliability passes through (UNKNOWN identity).
    assert await effective_credibility_of(session, fid) == pytest.approx(0.8)

    # A later alignment pass flags the claim self-serving -> credibility is discounted.
    await execute_cypher(
        session,
        f"MATCH (f:Fact {cypher_map({'id': str(fid)})}) "
        f"SET f.interest_alignment = '{InterestAlignment.SELF_SERVING}'",
    )
    await session.commit()
    discounted = await effective_credibility_of(session, fid)
    assert discounted is not None and discounted < 0.8
