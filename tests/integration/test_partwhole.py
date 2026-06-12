"""Phase 2 G2.5 integration test — the meronymy-induction operator end to end.

Real Postgres+AGE with both LLMs (extractor + meronymy detector) mocked. The extractor
(G2.2) seeds a box with ``Actor``/``Object`` entities; the inducer detects ``directPartOf``
candidates from each proposition, writes the typed edges, rebuilds the ``partOf`` transitive
closure (component-integral only), and derives a fact's abstraction level from its
subject-role referent's partonomy depth (§14).
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import case_box
from iknos.core.anchor import EntityLinker
from iknos.core.extract import ExtractInput, Extractor
from iknos.core.partwhole import LevelReading, MeronymyInducer
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher
from iknos.domain.loader import load_pack
from iknos.domain.packs import bundled_pack
from iknos.types.edges import AttachmentProvenance
from iknos.types.nodes import Proposition, Span

pytestmark = pytest.mark.asyncio


class _DispatchLLM:
    """Mock LLM returning a payload keyed on a substring of the statement (concurrent-safe)."""

    model = "test-model"

    def __init__(self, key: str, table: dict[str, list[dict]]):
        self._key = key
        self._table = table

    async def guided_complete(self, messages, schema, sampling, *, usage_out=None):
        statement = messages[1]["content"]
        for needle, payload in self._table.items():
            if needle in statement:
                return {self._key: payload}
        return {self._key: []}


async def _seed_proposition(session: AsyncSession, *, text_: str) -> tuple[Proposition, Span]:
    doc_id = uuid.uuid4()
    await execute_cypher(session, f"CREATE (:Document {cypher_map({'id': str(doc_id)})})")
    span = Span(id=uuid.uuid4(), document_id=doc_id, start=0, end=len(text_))
    await execute_cypher(
        session,
        "CREATE (:Span "
        + cypher_map(
            {"id": str(span.id), "document_id": str(doc_id), "start": 0, "end": len(text_)}
        )
        + ")",
    )
    prop = Proposition(id=uuid.uuid4(), text=text_)
    await execute_cypher(
        session, "CREATE (:Proposition " + cypher_map({"id": str(prop.id), "text": prop.text}) + ")"
    )
    await execute_cypher(
        session,
        f"MATCH (p:Proposition {cypher_map({'id': str(prop.id)})}), "
        f"(s:Span {cypher_map({'id': str(span.id)})}) CREATE (p)-[:EVIDENCED_BY]->(s)",
    )
    await session.commit()
    return prop, span


async def _extract(session: AsyncSession, box, table, props) -> None:
    ex = Extractor(_DispatchLLM("entities", table), concurrency=4)  # type: ignore[arg-type]
    await ex.extract_propositions(
        session, [ExtractInput(proposition=p, span_ids=[s.id]) for p, s in props], box
    )


def _inducer(table) -> MeronymyInducer:
    return MeronymyInducer(_DispatchLLM("relations", table), concurrency=4)  # type: ignore[arg-type]


async def _fact_id(session: AsyncSession, prop: Proposition, box) -> uuid.UUID:
    bx = cypher_map({"box": str(box.id)})
    rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {bx})-[:EVIDENCED_BY]->(p:Proposition {cypher_map({'id': str(prop.id)})}) "
        "RETURN f.id",
        returns="fid agtype",
    )
    return uuid.UUID(str(rows[0][0]).strip('"'))


async def _count_edges(session: AsyncSession, box, label: str) -> int:
    bx = cypher_map({"box": str(box.id)})
    rows = await execute_cypher(
        session, f"MATCH (a {bx})-[r:{label}]->(b {bx}) RETURN count(r)", returns="n agtype"
    )
    return int(str(rows[0][0]))


async def test_induce_builds_closure_and_derives_level(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("partwhole-chain", "1", "test", 0.8)
    # A four-level component-integral chain spread over propositions; each entity recurs
    # across propositions (fresh nodes), exercising canonical-by-label resolution.
    extract_table = {
        "contains": [
            {"label": "shaft", "type": "component", "kind": "object", "role": "subject"},
            {"label": "gearbox", "type": "assembly", "kind": "object", "role": "object"},
        ],
        "holds": [
            {"label": "bearing", "type": "component", "kind": "object", "role": "subject"},
            {"label": "shaft", "type": "component", "kind": "object", "role": "object"},
        ],
        "carries": [
            {"label": "roller", "type": "component", "kind": "object", "role": "subject"},
            {"label": "bearing", "type": "component", "kind": "object", "role": "object"},
        ],
        "spalled": [{"label": "roller", "type": "component", "kind": "object", "role": "subject"}],
    }
    p1 = await _seed_proposition(session, text_="The gearbox contains a shaft.")
    p2 = await _seed_proposition(session, text_="The shaft holds a bearing.")
    p3 = await _seed_proposition(session, text_="The bearing carries a roller.")
    p4 = await _seed_proposition(session, text_="The roller spalled.")
    await _extract(session, box, extract_table, [p1, p2, p3, p4])

    meronymy_table = {
        "contains": [
            {"child": "shaft", "parent": "gearbox", "meronymy_type": "component-integral"}
        ],
        "holds": [{"child": "bearing", "parent": "shaft", "meronymy_type": "component-integral"}],
        "carries": [
            {"child": "roller", "parent": "bearing", "meronymy_type": "component-integral"}
        ],
    }
    inducer = _inducer(meronymy_table)
    result = await inducer.induce_box(session, box.id)

    assert len(result.direct) == 3
    assert result.cyclic == frozenset()
    # directPartOf: 3 steps. partOf closure: 3 + 2 + 1 = 6 ancestor pairs.
    assert await _count_edges(session, box, "directPartOf") == 3
    assert result.part_of_count == 6
    assert await _count_edges(session, box, "partOf") == 6

    # Derived level of the "The roller spalled." fact = roller's partonomy depth = 3, even
    # though that fact's roller is a different fresh node than the one in the hierarchy. With
    # no pack loaded the level source is the induced partonomy (provenance INDUCED, §14).
    f4 = await _fact_id(session, p4[0], box)
    assert await inducer.fact_level(session, box.id, f4) == [
        LevelReading(3, AttachmentProvenance.INDUCED)
    ]
    # The gearbox fact attaches at the coarsest level (no parent).
    f1 = await _fact_id(session, p1[0], box)
    assert await inducer.fact_level(session, box.id, f1) == [
        LevelReading(1, AttachmentProvenance.INDUCED)  # shaft is p1's subject
    ]

    # Idempotent re-run: settled propositions skipped, no duplicate edges.
    again = await inducer.induce_box(session, box.id)
    assert again.direct == []
    assert await _count_edges(session, box, "directPartOf") == 3
    assert await _count_edges(session, box, "partOf") == 6


async def test_non_transitive_subtype_excluded_from_rollup(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("partwhole-membercoll", "1", "test", 0.8)
    extract_table = {
        "includes": [
            {"label": "gearbox", "type": "assembly", "kind": "object", "role": "subject"},
            {"label": "fleet", "type": "collection", "kind": "object", "role": "object"},
        ]
    }
    p1 = await _seed_proposition(session, text_="The fleet includes a gearbox.")
    await _extract(session, box, extract_table, [p1])

    # A member-collection relation is tagged but NOT transitivity-safe -> no partOf roll-up.
    inducer = _inducer(
        {
            "includes": [
                {"child": "gearbox", "parent": "fleet", "meronymy_type": "member-collection"}
            ]
        }
    )
    result = await inducer.induce_box(session, box.id)

    assert len(result.direct) == 1
    assert await _count_edges(session, box, "directPartOf") == 1
    # Excluded from the closure (§14): no partOf edge, gearbox has no partonomy ancestor.
    assert result.part_of_count == 0
    assert await _count_edges(session, box, "partOf") == 0


async def test_level_read_follows_confirmed_anchor_into_pack_partonomy(
    session: AsyncSession,
) -> None:
    """G2.8 slice 2: an anchored entity's level is read off the pack taxonomy depth (§14).

    The pack's ``roller -> bearing -> pump`` puts "Roller" at partonomy depth 2. A case "roller"
    confirm-anchors to it, so its fact's level resolves to 2 with provenance ANCHORED — read off
    the **taxonomy**, with no induced meronymy in the case box at all. An out-of-taxonomy
    "gearbox" falls back to the (empty) induced partonomy: depth 0, provenance INDUCED.
    """
    await bootstrap_session(session)
    pack = bundled_pack("pump_basic")
    await load_pack(session, pack)
    await session.commit()

    box = case_box("partwhole-anchored-level", "1", "test", 0.8)
    extract_table = {
        "roller": [{"label": "roller", "type": "component", "kind": "object", "role": "subject"}],
        "gearbox": [{"label": "gearbox", "type": "assembly", "kind": "object", "role": "subject"}],
    }
    p_roller = await _seed_proposition(session, text_="The roller spalled.")
    p_gearbox = await _seed_proposition(session, text_="The gearbox vibrated.")
    await _extract(session, box, extract_table, [p_roller, p_gearbox])

    # No induce run: the case box has zero partOf edges, so any non-zero level is anchor-driven.
    await EntityLinker().anchor_box(session, box.id, pack_box_ids=[pack.box_id])
    assert await _count_edges(session, box, "partOf") == 0

    inducer = _inducer({})
    f_roller = await _fact_id(session, p_roller[0], box)
    assert await inducer.fact_level(session, box.id, f_roller) == [
        LevelReading(2, AttachmentProvenance.ANCHORED)
    ]
    # The out-of-taxonomy gearbox has no anchor and no induced structure -> INDUCED depth 0.
    f_gearbox = await _fact_id(session, p_gearbox[0], box)
    assert await inducer.fact_level(session, box.id, f_gearbox) == [
        LevelReading(0, AttachmentProvenance.INDUCED)
    ]
