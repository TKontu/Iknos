"""Phase 2 G2.4 integration test — the reference-binding operator end to end.

Real Postgres+AGE with both LLMs (extractor + detector) mocked. The extractor (G2.2) seeds
a box with Facts whose ``Actor``/``Object`` nodes are the in-graph entities; the binder
detects each proposition's ``Mention``s and binds them through the scoped cascade — a
``CONFIRMED`` ``REFERS_TO`` for a single exact referent, ``CANDIDATE`` edges for an
ambiguous one, and an unresolved (no-edge) pronoun — marking the dependent propositions
``provisional`` wherever the binding stays open (§3.1).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import case_box
from iknos.core.extract import ExtractInput, Extractor
from iknos.core.reference import BindingStage, ReferenceBinder
from iknos.db.age import bootstrap_session, cypher_map, execute_cypher, parse_agtype_map
from iknos.domain.loader import load_pack
from iknos.domain.packs import bundled_pack
from iknos.types.edges import BindingState
from iknos.types.epistemic import decode_provisional_reasons
from iknos.types.nodes import Proposition, Span

pytestmark = pytest.mark.asyncio


class _DispatchLLM:
    """Mock LLM that returns a payload keyed on a substring of the statement.

    Both the extractor and the binder infer concurrently, so a fixed return list cannot be
    aligned to proposition order — dispatch on the statement content instead. ``key`` selects
    which response field the operator reads (``entities`` vs ``mentions``).
    """

    model = "test-model"

    def __init__(self, key: str, table: dict[str, list[dict]]):
        self._key = key
        self._table = table

    async def guided_complete(self, messages, schema, sampling):
        statement = messages[1]["content"]
        for needle, payload in self._table.items():
            if needle in statement:
                return {self._key: payload}
        return {self._key: []}


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
        session,
        "CREATE (:Proposition " + cypher_map({"id": str(prop.id), "text": prop.text}) + ")",
    )
    await execute_cypher(
        session,
        f"MATCH (p:Proposition {cypher_map({'id': str(prop.id)})}), "
        f"(s:Span {cypher_map({'id': str(span.id)})}) "
        "CREATE (p)-[:EVIDENCED_BY]->(s)",
    )
    await session.commit()
    return span, prop


async def _extract(session: AsyncSession, box, table: dict[str, list[dict]], props) -> None:
    ex = Extractor(_DispatchLLM("entities", table), concurrency=4)  # type: ignore[arg-type]
    await ex.extract_propositions(
        session, [ExtractInput(proposition=p, span_ids=[s.id]) for s, p in props], box
    )


def _binder(table: dict[str, list[dict]]) -> ReferenceBinder:
    return ReferenceBinder(_DispatchLLM("mentions", table), concurrency=4)  # type: ignore[arg-type]


async def _refers_to(session: AsyncSession, box) -> list[tuple[str, str]]:
    """All REFERS_TO edges in the box as (state, strength) string pairs."""
    bx = cypher_map({"box": str(box.id)})
    rows = await execute_cypher(
        session,
        f"MATCH (m:Mention {bx})-[r:REFERS_TO]->(e {bx}) RETURN r.state, r.strength",
        returns="state agtype, strength agtype",
    )
    return [(str(st).strip('"'), str(strn)) for st, strn in rows]


async def _provisional(session: AsyncSession, prop: Proposition) -> str:
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition {cypher_map({'id': str(prop.id)})}) RETURN p.provisional",
        returns="prov agtype",
    )
    # An unset property reads back as agtype null, which the driver surfaces as either
    # Python None or the string "null" depending on the AGE/agtype codec — normalize both
    # to "null" so the assertions are robust to the representation (the codebase idiom,
    # cf. db.age handling and provenance.audit).
    raw = rows[0][0]
    return "null" if raw is None or str(raw) == "null" else str(raw)


async def _provisional_reasons(session: AsyncSession, prop: Proposition) -> list[str]:
    """The R8 reason set persisted on the proposition (decoded via the proven props path)."""
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition {cypher_map({'id': str(prop.id)})}) RETURN properties(p)",
        returns="props agtype",
    )
    return decode_provisional_reasons(parse_agtype_map(rows[0][0]).get("provisional_reasons"))


async def test_binder_confirms_proper_name_and_marks_clean(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("bind-confirm", "1", "test", 0.8)
    # Two facts introduce the named entity "bearing 3"; the binder then binds a "bearing 3"
    # mention exactly to it.
    extract_table = {
        "failed": [
            {"label": "bearing 3", "type": "component", "kind": "object", "role": "subject"}
        ],
        "inspected": [
            {"label": "bearing 3", "type": "component", "kind": "object", "role": "object"},
            {"label": "technician", "type": "person", "kind": "actor", "role": "subject"},
        ],
    }
    p1 = await _seed_proposition(session, text_="Bearing 3 failed.")
    p2 = await _seed_proposition(session, text_="The technician inspected bearing 3.")
    await _extract(session, box, extract_table, [p1, p2])

    binder = _binder(
        {
            "inspected": [{"surface": "bearing 3", "mention_type": "proper", "kind": "object"}],
            # "failed" proposition has no referring mention -> empty.
        }
    )
    result = await binder.bind_box(session, box.id)

    # One confirmed binding; the other proposition has no mention.
    confirmed = [b for b in result.bound if b.state is BindingState.CONFIRMED]
    assert len(confirmed) == 1
    edges = await _refers_to(session, box)
    assert len(edges) == 1
    assert edges[0][0] == str(BindingState.CONFIRMED)
    assert 0.0 <= float(edges[0][1]) <= 1.0

    # A confirmed binding does not make its proposition provisional.
    assert result.provisional_propositions == []
    assert await _provisional(session, p2[1]) == "null"
    assert await _provisional_reasons(session, p2[1]) == []

    # The bind Action is joinable (§10.1).
    act = await session.execute(
        text(
            "SELECT outputs FROM actions WHERE actor = 'reference-binder' "
            "AND inputs->>'proposition' = :p"
        ),
        {"p": str(p2[1].id)},
    )
    assert len(act.one().outputs["confirmed"]) == 1

    # Idempotent re-run: already-bound propositions are skipped, no duplicate edges/mentions.
    again = await binder.bind_box(session, box.id)
    assert again.bound == []
    assert len(await _refers_to(session, box)) == 1
    bx = cypher_map({"box": str(box.id)})
    mentions = await execute_cypher(
        session, f"MATCH (m:Mention {bx}) RETURN count(m)", returns="n agtype"
    )
    assert int(str(mentions[0][0])) == 1


async def test_binder_keeps_ambiguous_open_and_marks_provisional(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("bind-ambiguous", "1", "test", 0.8)
    # Two distinct named bearings exist; a later "the bearing" definite description is
    # ambiguous between them -> candidate edges, proposition provisional.
    extract_table = {
        "Bearing 3 overheated": [
            {"label": "bearing 3", "type": "component", "kind": "object", "role": "subject"}
        ],
        "Bearing 4 overheated": [
            {"label": "bearing 4", "type": "component", "kind": "object", "role": "subject"}
        ],
        "replaced": [
            {"label": "the bearing", "type": "component", "kind": "object", "role": "object"},
            {"label": "technician", "type": "person", "kind": "actor", "role": "subject"},
        ],
    }
    p1 = await _seed_proposition(session, text_="Bearing 3 overheated.")
    p2 = await _seed_proposition(session, text_="Bearing 4 overheated.")
    p3 = await _seed_proposition(session, text_="The technician replaced the bearing.")
    await _extract(session, box, extract_table, [p1, p2, p3])

    binder = _binder(
        {"replaced": [{"surface": "the bearing", "mention_type": "definite", "kind": "object"}]}
    )
    result = await binder.bind_box(session, box.id)

    # The "the bearing" mention ties between bearing 3 and bearing 4 -> two candidate edges.
    edges = await _refers_to(session, box)
    assert len(edges) == 2
    assert all(st == str(BindingState.CANDIDATE) for st, _ in edges)

    # The dependent proposition is provisional; no binding is confirmed.
    assert p3[1].id in result.provisional_propositions
    assert await _provisional(session, p3[1]) == "true"
    assert await _provisional_reasons(session, p3[1]) == ["unresolved_reference"]
    assert all(b.state is BindingState.CANDIDATE for b in result.bound if b.targets)


async def test_binder_leaves_pronoun_unresolved_and_provisional(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("bind-pronoun", "1", "test", 0.8)
    extract_table = {
        "overheated": [{"label": "pump", "type": "equipment", "kind": "object", "role": "subject"}]
    }
    p1 = await _seed_proposition(session, text_="It overheated.")
    await _extract(session, box, extract_table, [p1])

    binder = _binder({"overheated": [{"surface": "It", "mention_type": "pronoun", "kind": None}]})
    result = await binder.bind_box(session, box.id)

    # A bare pronoun blocks to no in-graph referent (the discourse-antecedent seam) -> no
    # REFERS_TO edge, but the Mention is recorded and its proposition is provisional.
    assert await _refers_to(session, box) == []
    assert len(result.bound) == 1
    assert result.bound[0].state is None
    assert p1[1].id in result.provisional_propositions
    assert await _provisional(session, p1[1]) == "true"
    assert await _provisional_reasons(session, p1[1]) == ["unresolved_reference"]

    bx = cypher_map({"box": str(box.id)})
    mentions = await execute_cypher(
        session, f"MATCH (m:Mention {bx}) RETURN count(m)", returns="n agtype"
    )
    assert int(str(mentions[0][0])) == 1


async def test_binder_falls_through_to_taxonomy_stage(session: AsyncSession) -> None:
    """G2.4 cascade tail: a mention the box cannot bind binds to a pack taxonomy node (§3.1)."""
    await bootstrap_session(session)

    # The pack supplies the taxonomy "Roller" the mention "the roller" exact-matches.
    pack = bundled_pack("pump_basic")
    await load_pack(session, pack)
    await session.commit()

    box = case_box("bind-taxonomy", "1", "test", 0.8)
    # Only one proposition; its own "roller" entity is excluded from its referent pool (no
    # self-binding), so the in-graph stage finds nothing and the cascade falls to the taxonomy.
    extract_table = {
        "replaced": [{"label": "roller", "type": "component", "kind": "object", "role": "subject"}]
    }
    p1 = await _seed_proposition(session, text_="The roller was replaced.")
    await _extract(session, box, extract_table, [p1])

    binder = _binder(
        {"replaced": [{"surface": "the roller", "mention_type": "definite", "kind": "object"}]}
    )
    result = await binder.bind_box(session, box.id)

    # The mention confirm-binds to the taxonomy node via the taxonomy stage.
    assert len(result.bound) == 1
    bound = result.bound[0]
    assert bound.state is BindingState.CONFIRMED
    assert bound.stage is BindingStage.TAXONOMY
    # A confirmed binding (even cross-box to the taxonomy) leaves its proposition non-provisional.
    assert result.provisional_propositions == []
    assert await _provisional(session, p1[1]) == "null"
    assert await _provisional_reasons(session, p1[1]) == []

    # The REFERS_TO points cross-box at the pack's "Roller" Object, state CONFIRMED.
    rows = await execute_cypher(
        session,
        f"MATCH (m:Mention {{box: '{box.id}'}})-[r:REFERS_TO]->(t:Object) "
        "RETURN r.state, t.label, t.box",
        returns="state agtype, label agtype, tbox agtype",
    )
    assert len(rows) == 1
    state, label, tbox = rows[0]
    assert str(state).strip('"') == str(BindingState.CONFIRMED)
    assert str(label).strip('"') == "Roller"
    assert str(tbox).strip('"') == str(pack.box_id)  # the target lives in the pack box

    # The bind Action records the cascade-tail binding in its `taxonomy` output (§10.1).
    act = await session.execute(
        text(
            "SELECT outputs FROM actions WHERE actor = 'reference-binder' "
            "AND inputs->>'proposition' = :p"
        ),
        {"p": str(p1[1].id)},
    )
    outputs = act.one().outputs
    assert len(outputs["taxonomy"]) == 1
    assert len(outputs["confirmed"]) == 1

    # Idempotent re-run: the settled proposition is skipped, no new binding.
    again = await binder.bind_box(session, box.id)
    assert again.bound == []
