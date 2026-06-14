"""W8 — the Cypher chokepoint builder: pure rendering, escaping, and enum validation (no DB).

Mirrors ``test_age_cypher_map``'s discipline: the query-text assembly is pure, so it is pinned here
without an engine. These tests lock the *rendered output* of the fragments and the builder so the
migration of the ~50 hand-rolled f-string call sites is provably equivalent and a future refactor
cannot silently change a query.
"""

from __future__ import annotations

import uuid

from iknos.db.cypher import (
    CypherQuery,
    EdgeType,
    NodeLabel,
    Raw,
    lit,
    lit_list,
    node,
    rel,
)

# --- scalar escaping (lit) --------------------------------------------------------------------


def test_lit_scalar_types() -> None:
    assert lit("plain") == "'plain'"
    assert lit("O'Brien") == "'O\\'Brien'"  # single-quote escaped, like cypher_map
    assert lit("back\\slash") == "'back\\\\slash'"  # backslash escaped first
    assert lit(True) == "true"
    assert lit(False) == "false"
    assert lit(3) == "3"
    assert lit(1.5) == "1.5"
    assert lit(None) == "null"


def test_lit_strenum_serializes_to_value() -> None:
    # A StrEnum is a str subclass, so it routes through the string branch and serializes to its
    # value (not "NodeLabel.SPAN") — exactly how the old f-strings embedded enum values.
    assert lit(NodeLabel.SPAN) == "'Span'"
    assert lit(EdgeType.SUPPORTS) == "'SUPPORTS'"


def test_lit_uuid_and_list() -> None:
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert lit(str(u)) == f"'{u}'"
    assert lit_list(["a", "b"]) == "['a', 'b']"
    assert lit_list([1, 2, 3]) == "[1, 2, 3]"


# --- pattern fragments (node / rel) -----------------------------------------------------------


def test_node_variants() -> None:
    assert node("p") == "(p)"
    assert node("b", NodeLabel.BOX) == "(b:Box)"
    assert node("s", NodeLabel.SPAN, {"id": "abc"}) == "(s:Span {id: 'abc'})"
    assert node("", props={"id": "x"}) == "( {id: 'x'})"
    # numeric and string props in one map (the ingest span-by-level read shape)
    assert (
        node("s", NodeLabel.SPAN, {"document_id": "d1", "level": 0})
        == "(s:Span {document_id: 'd1', level: 0})"
    )


def test_rel_variants() -> None:
    assert rel(EdgeType.INVOLVES) == "-[:INVOLVES]->"
    assert rel(EdgeType.SAME_AS, var="r") == "-[r:SAME_AS]->"
    assert rel(var="r", directed=False) == "-[r]-"
    assert rel(EdgeType.DIRECT_PART_OF, var="r") == "-[r:directPartOf]->"


# --- the clause builder -----------------------------------------------------------------------


def test_simple_read_by_id() -> None:
    q = CypherQuery().match(node("b", NodeLabel.BOX, {"id": "box-1"})).return_("properties(b)")
    assert q.render() == "MATCH (b:Box {id: 'box-1'}) RETURN properties(b)"


def test_filtered_read_with_order_by() -> None:
    q = (
        CypherQuery()
        .match(node("b", NodeLabel.BOX))
        .where("b.status = " + lit("active"), "b.valid_to IS NULL")
        .return_("properties(b)")
        .order_by("b.reliability_prior DESC")
    )
    assert q.render() == (
        "MATCH (b:Box) WHERE b.status = 'active' AND b.valid_to IS NULL "
        "RETURN properties(b) ORDER BY b.reliability_prior DESC"
    )


def test_traversal_with_optional_match_and_aggregation() -> None:
    span = node("s", NodeLabel.SPAN, {"id": "s-1"})
    q = (
        CypherQuery()
        .match(node("p", NodeLabel.PROPOSITION) + rel(EdgeType.EVIDENCED_BY) + span)
        .optional_match(node("p") + rel(var="r", directed=False) + node())
        .with_("p, count(r) AS degree")
        .where("degree > 1")
        .return_("count(p)")
    )
    assert q.render() == (
        "MATCH (p:Proposition)-[:EVIDENCED_BY]->(s:Span {id: 's-1'}) "
        "OPTIONAL MATCH (p)-[r]-() WITH p, count(r) AS degree WHERE degree > 1 RETURN count(p)"
    )


def test_edge_set_with_temporal_guard() -> None:
    q = (
        CypherQuery()
        .match(
            node("x", props={"id": "a"})
            + rel(EdgeType.SAME_AS, var="r")
            + node("y", props={"id": "b"})
        )
        .where("r.valid_to IS NULL")
        .set("r.valid_to = " + lit("2026-01-01T00:00:00"))
    )
    assert q.render() == (
        "MATCH (x {id: 'a'})-[r:SAME_AS]->(y {id: 'b'}) WHERE r.valid_to IS NULL "
        "SET r.valid_to = '2026-01-01T00:00:00'"
    )


def test_create_vertex_and_detach_delete() -> None:
    create = (
        CypherQuery()
        .create(node("p", NodeLabel.PROPOSITION, {"id": "p1", "text": "hi"}))
        .return_("p")
    )
    assert create.render() == "CREATE (p:Proposition {id: 'p1', text: 'hi'}) RETURN p"

    delete = (
        CypherQuery()
        .match(
            node("p", NodeLabel.PROPOSITION)
            + rel(EdgeType.EVIDENCED_BY)
            + node("s", NodeLabel.SPAN, {"id": "s1"})
        )
        .detach_delete("p")
    )
    assert (
        delete.render()
        == "MATCH (p:Proposition)-[:EVIDENCED_BY]->(s:Span {id: 's1'}) DETACH DELETE p"
    )


def test_in_list_read() -> None:
    q = (
        CypherQuery()
        .match(node("p", NodeLabel.PROPOSITION))
        .where("p.id IN " + lit_list(["a", "b"]))
        .return_("p.id, properties(p)")
    )
    assert q.render() == "MATCH (p:Proposition) WHERE p.id IN ['a', 'b'] RETURN p.id, properties(p)"


def test_raw_property_join_is_inlined_verbatim() -> None:
    # A graph property-to-property join: the value is another variable's property, not data, so it
    # must inline verbatim (not be escaped as a string literal).
    assert node("b", NodeLabel.BOX, {"id": Raw("f.box")}) == "(b:Box {id: f.box})"
    # Non-raw values in the same map are still escaped.
    assert (
        node("b", NodeLabel.BOX, {"id": Raw("f.box"), "name": "x"})
        == "(b:Box {id: f.box, name: 'x'})"
    )


def test_enum_vocabularies_complete() -> None:
    # Guard against an accidental rename/removal of a graph label or edge type.
    assert {n.value for n in NodeLabel} == {
        "Box",
        "Document",
        "Span",
        "Proposition",
        "Fact",
        "Actor",
        "Object",
        "Mention",
        "DeductiveConclusion",
        "InductiveConclusion",
        "Hypothesis",
    }
    assert {e.value for e in EdgeType} == {
        "INVOLVES",
        "EVIDENCED_BY",
        "SAME_AS",
        "ANCHORS_TO",
        "REFERS_TO",
        "directPartOf",
        "partOf",
        "DERIVED_FROM",
        "MEMBER_OF",
        "SUPPORTS",
        "REFUTES",
    }
