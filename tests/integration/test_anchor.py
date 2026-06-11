"""Phase 2 G2.8 integration test — the entity-linking / taxonomy-anchoring operator end to end.

Real Postgres+AGE with the extractor's LLM mocked. A domain pack (``pump_basic``) loads its
curated taxonomy ``Object`` nodes into a reference Box; the extractor (G2.2) seeds a case Box
with fresh ``Actor``/``Object`` nodes; the linker scores each case entity against the active
taxonomy and writes ``ANCHORS_TO`` edges — ``CONFIRMED`` on an exact match, ``CANDIDATE`` on a
lexical tie, none for an out-of-taxonomy entity — then exposes the confirmed targets and the
coverage metric (§14).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import case_box
from iknos.core.anchor import EntityLinker
from iknos.core.extract import ExtractInput, Extractor
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.domain.loader import list_active_packs, load_pack
from iknos.domain.packs import bundled_pack
from iknos.types.edges import AnchorState
from iknos.types.nodes import Proposition, Span

pytestmark = pytest.mark.asyncio


class _DispatchLLM:
    """Mock extractor LLM: returns entities keyed on a substring of the statement."""

    model = "test-model"

    def __init__(self, table: dict[str, list[dict]]):
        self._table = table

    async def guided_complete(self, messages, schema, sampling):
        statement = messages[1]["content"]
        for needle, entities in self._table.items():
            if needle in statement:
                return {"entities": entities}
        return {"entities": []}


async def _seed_proposition(session: AsyncSession, *, text_: str) -> tuple[Span, Proposition]:
    """Create a Document + Span + Proposition (EVIDENCED_BY the Span) the extractor reads."""
    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": text_},
    )
    await execute_cypher(session, f"CREATE (:Document {cypher_map({'id': str(doc_id)})})")
    span = Span(id=uuid.uuid4(), document_id=doc_id, start=0, end=len(text_))
    await execute_cypher(
        session,
        "CREATE (:Span "
        + cypher_map(
            {"id": str(span.id), "document_id": str(doc_id), "start": span.start, "end": span.end}
        )
        + ")",
    )
    prop = Proposition(id=uuid.uuid4(), text=text_)
    await execute_cypher(
        session, "CREATE (:Proposition " + cypher_map({"id": str(prop.id), "text": prop.text}) + ")"
    )
    await session.commit()
    return span, prop


async def _extract(session: AsyncSession, box, llm: _DispatchLLM, props) -> None:
    ex = Extractor(llm, concurrency=4)  # type: ignore[arg-type]
    await ex.extract_propositions(
        session, [ExtractInput(proposition=p, span_ids=[s.id]) for s, p in props], box
    )


async def _anchor_count(session: AsyncSession, box) -> int:
    bx = cypher_map({"box": str(box.id)})
    rows = await execute_cypher(
        session, f"MATCH (e {bx})-[r:ANCHORS_TO]->(t) RETURN count(r)", returns="n agtype"
    )
    return int(str(rows[0][0]))


async def test_linker_confirms_ties_and_measures_coverage(session: AsyncSession) -> None:
    await bootstrap_session(session)

    # The pack's curated taxonomy is the anchor target population.
    pack = bundled_pack("pump_basic")
    await load_pack(session, pack)
    await session.commit()
    # The default (pre-Phase-6) activation lookup sees it (the no-arg scope path).
    assert str(pack.box_id) in {p["id"] for p in await list_active_packs(session)}

    box = case_box("anchor-coverage", "1", "test", 0.8)
    llm = _DispatchLLM(
        {
            "roller": [{"label": "roller", "type": "part", "kind": "object", "role": "subject"}],
            "pump": [{"label": "pump", "type": "equipment", "kind": "object", "role": "subject"}],
            "gearbox": [
                {"label": "gearbox", "type": "assembly", "kind": "object", "role": "subject"}
            ],
        }
    )
    p1 = await _seed_proposition(session, text_="The roller spalled.")
    p2 = await _seed_proposition(session, text_="The pump tripped.")
    p3 = await _seed_proposition(session, text_="The gearbox vibrated.")
    await _extract(session, box, llm, [p1, p2, p3])

    linker = EntityLinker()
    # Scope explicitly to this pack (the investigation-activation seam) for a deterministic run.
    result = await linker.anchor_box(session, box.id, pack_box_ids=[pack.box_id])

    # "roller" exact-matches the taxonomy "Roller" → a single CONFIRMED anchor.
    assert len(result.confirmed) == 1
    # "pump" is contained in both "Centrifugal pump" and "Pump housing" → a tie → two CANDIDATEs.
    assert len(result.candidate) == 2
    # "gearbox" is out of taxonomy → no edge at all.
    assert await _anchor_count(session, box) == 3

    # The confirmed edge points at the taxonomy "Roller" node, carries state + annotations.
    confirmed = await execute_cypher(
        session,
        f"MATCH (e {{box: '{box.id}'}})-[r:ANCHORS_TO]->(t) "
        f"WHERE r.state = '{AnchorState.CONFIRMED}' RETURN t.label, r.strength, r.support_count",
        returns="label agtype, strength agtype, sc agtype",
    )
    assert len(confirmed) == 1
    label, strength, sc = confirmed[0]
    assert str(label).strip('"') == "Roller"
    assert 0.0 <= float(str(strength)) <= 1.0
    assert int(str(sc)) == 1

    # The two candidate anchors are the lexically-tied wholes.
    cand = await execute_cypher(
        session,
        f"MATCH (e {{box: '{box.id}'}})-[r:ANCHORS_TO]->(t) "
        f"WHERE r.state = '{AnchorState.CANDIDATE}' RETURN t.label",
        returns="label agtype",
    )
    assert {str(r[0]).strip('"') for r in cand} == {"Centrifugal pump", "Pump housing"}

    # anchored_targets exposes only the confirmed anchor; coverage = 1 confirmed of 3 entities.
    targets = await linker.anchored_targets(session, box.id)
    assert len(targets) == 1
    cov = await linker.coverage(session, box.id)
    assert cov.total == 3
    assert cov.anchored == 1
    assert cov.fraction == pytest.approx(1 / 3)
    # The run-time coverage agrees with the standalone read.
    assert (result.coverage.total, result.coverage.anchored) == (3, 1)

    # The anchor Action is joinable (§10.1).
    act = await session.execute(
        text("SELECT outputs FROM actions WHERE actor = 'entity-linker' AND inputs->>'box' = :b"),
        {"b": str(box.id)},
    )
    rec = act.one()
    assert len(rec.outputs["confirmed"]) == 1
    assert len(rec.outputs["candidate"]) == 2

    # Idempotent re-run: deterministic recompute upserts, no duplicate edges, same coverage.
    again = await linker.anchor_box(session, box.id, pack_box_ids=[pack.box_id])
    assert len(again.confirmed) == 1
    assert len(again.candidate) == 2
    assert await _anchor_count(session, box) == 3
    assert (await linker.coverage(session, box.id)).anchored == 1
