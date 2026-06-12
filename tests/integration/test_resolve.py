"""Phase 2 G2.3 integration test — the entity-resolution operator end to end.

Real Postgres+AGE with the extractor's LLM mocked. The extractor (G2.2) seeds a box with
Facts whose Actor/Object nodes are **fresh, un-deduplicated** (two mentions of the same
entity are two nodes); the resolver scores blocked pairs and writes ``SAME_AS`` edges —
``CONFIRMED`` above the bar, ``CANDIDATE`` below — then exposes the canonical components.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import case_box
from iknos.core.anchor import EntityLinker
from iknos.core.extract import ExtractInput, Extractor
from iknos.core.resolve import Resolver
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.domain.loader import load_pack
from iknos.domain.packs import bundled_pack
from iknos.types.edges import SameAsState
from iknos.types.nodes import Proposition, Span

pytestmark = pytest.mark.asyncio


class _DispatchLLM:
    """Mock LLM that returns entities keyed on a substring of the statement.

    The extractor infers concurrently, so a fixed return list cannot be aligned to
    proposition order — dispatch on the statement content instead.
    """

    model = "test-model"

    def __init__(self, table: dict[str, list[dict]]):
        self._table = table

    async def guided_complete(self, messages, schema, sampling, *, usage_out=None):
        statement = messages[1]["content"]
        for needle, entities in self._table.items():
            if needle in statement:
                return {"entities": entities}
        return {"entities": []}


async def _seed_proposition(session: AsyncSession, *, text_: str) -> tuple[Span, Proposition]:
    """Create a Document + Span + Proposition (EVIDENCED_BY the Span) the extractor reads."""
    doc_id = uuid.uuid4()
    raw = text_
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


async def _same_as_count(session: AsyncSession, box) -> int:
    bx = cypher_map({"box": str(box.id)})
    rows = await execute_cypher(
        session, f"MATCH (a {bx})-[r:SAME_AS]->(b {bx}) RETURN count(r)", returns="n agtype"
    )
    return int(str(rows[0][0]))


async def test_resolver_confirms_and_exposes_components(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("resolve-confirm", "1", "test", 0.8)
    llm = _DispatchLLM(
        {
            "restarted": [
                {"label": "the operator", "type": "person", "kind": "actor", "role": "subject"},
                {"label": "the pump", "type": "equipment", "kind": "object", "role": "object"},
            ],
            "inspected": [
                {"label": "operator", "type": "person", "kind": "actor", "role": "subject"},
                {"label": "pump", "type": "equipment", "kind": "object", "role": "object"},
            ],
        }
    )
    p1 = await _seed_proposition(session, text_="The operator restarted the pump.")
    p2 = await _seed_proposition(session, text_="The operator inspected the pump.")
    await _extract(session, box, llm, [p1, p2])

    # Two fresh operator Actors and two fresh pump Objects exist before resolution.
    bx = cypher_map({"box": str(box.id)})
    actors = await execute_cypher(
        session, f"MATCH (a:Actor {bx}) RETURN count(a)", returns="n agtype"
    )
    assert int(str(actors[0][0])) == 2

    resolver = Resolver()
    result = await resolver.resolve_box(session, box.id)

    # Both duplicate pairs auto-merge (exact label + type + shared relational context).
    assert len(result.confirmed) == 2
    assert result.candidate == []

    # The SAME_AS edges carry state=confirmed, a strength, and the two §12 annotations.
    edges = await execute_cypher(
        session,
        f"MATCH (a {bx})-[r:SAME_AS]->(b {bx}) RETURN r.state, r.strength, r.support_count",
        returns="state agtype, strength agtype, sc agtype",
    )
    assert len(edges) == 2
    for st, strength, sc in edges:
        assert str(st).strip('"') == str(SameAsState.CONFIRMED)
        assert 0.0 <= float(str(strength)) <= 1.0
        assert int(str(sc)) == 1

    # Canonical components: operators and pumps, each a 2-member component.
    comps = await resolver.canonical_components(session, box.id)
    assert len(comps) == 2
    assert all(len(c.members) == 2 for c in comps)
    assert all(c.canonical == min(c.members, key=str) for c in comps)

    # The resolve Action is joinable (§10.1).
    act = await session.execute(
        text("SELECT outputs FROM actions WHERE actor = 'entity-resolver' AND inputs->>'box' = :b"),
        {"b": str(box.id)},
    )
    rec = act.one()
    assert len(rec.outputs["confirmed"]) == 2

    # Idempotent re-run: deterministic recompute upserts, no duplicate edges, same components.
    again = await resolver.resolve_box(session, box.id)
    assert len(again.confirmed) == 2
    assert await _same_as_count(session, box) == 2
    assert len(await resolver.canonical_components(session, box.id)) == 2


async def test_resolver_records_candidate_below_the_bar(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("resolve-candidate", "1", "test", 0.8)
    llm = _DispatchLLM(
        {
            "failed": [
                {"label": "the bearing", "type": "component", "kind": "object", "role": "subject"}
            ],
            "examined": [
                {"label": "technician", "type": "person", "kind": "actor", "role": "subject"},
                {"label": "bearing", "type": "component", "kind": "object", "role": "object"},
            ],
        }
    )
    p1 = await _seed_proposition(session, text_="The bearing failed.")
    p2 = await _seed_proposition(session, text_="The technician examined the bearing.")
    await _extract(session, box, llm, [p1, p2])

    resolver = Resolver()
    result = await resolver.resolve_box(session, box.id)

    # Exact label + type but disjoint context and no shared role -> candidate, not confirmed.
    assert result.confirmed == []
    assert len(result.candidate) == 1
    assert result.candidate[0].state is SameAsState.CANDIDATE

    bx = cypher_map({"box": str(box.id)})
    state = await execute_cypher(
        session, f"MATCH (a {bx})-[r:SAME_AS]->(b {bx}) RETURN r.state", returns="state agtype"
    )
    assert str(state[0][0]).strip('"') == str(SameAsState.CANDIDATE)

    # A candidate keeps entities separate: no canonical component is formed.
    assert await resolver.canonical_components(session, box.id) == []


async def test_canonical_components_folds_confirmed_anchor_as_identity(
    session: AsyncSession,
) -> None:
    """G2.8 slice 2: a confirm-anchored entity's canonical identity is its taxonomy node."""
    await bootstrap_session(session)

    # The pack supplies the taxonomy "Roller" the case "roller" exact-matches (anchor target).
    pack = bundled_pack("pump_basic")
    await load_pack(session, pack)
    await session.commit()

    box = case_box("resolve-anchor-fold", "1", "test", 0.8)
    llm = _DispatchLLM(
        {
            "roller": [
                {"label": "roller", "type": "component", "kind": "object", "role": "subject"}
            ],
            "gearbox": [
                {"label": "gearbox", "type": "assembly", "kind": "object", "role": "subject"}
            ],
        }
    )
    p1 = await _seed_proposition(session, text_="The roller spalled.")
    p2 = await _seed_proposition(session, text_="The gearbox vibrated.")
    await _extract(session, box, llm, [p1, p2])

    # Anchor (no resolve run): only "roller" confirm-anchors; "gearbox" is out of taxonomy.
    linker = EntityLinker()
    await linker.anchor_box(session, box.id, pack_box_ids=[pack.box_id])
    [(roller_node, roller_taxo)] = (await linker.anchored_targets(session, box.id)).items()

    comps = await Resolver().canonical_components(session, box.id)
    # The confirm-anchored "roller" is one canonical entity whose identity IS the taxonomy node
    # (anchor canonicalizes, §5.2/§14) — even with no SAME_AS edge. The out-of-taxonomy
    # "gearbox" is an un-anchored singleton, so it forms no component.
    assert len(comps) == 1
    comp = comps[0]
    assert comp.canonical == roller_taxo
    assert comp.anchored and comp.anchor == roller_taxo and not comp.anchor_conflict
    assert roller_node in comp.members
