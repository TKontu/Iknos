"""AGE label indexes: property GIN on vertices, endpoint btree on edges (G0.R2)

Revision ID: 0007_age_label_indexes
Revises: 0006_actions_target_span_index
Create Date: 2026-06-11

Touches AGE graph: yes (creates indexes on the graph's label tables)

Closes docs/gap_phase_0_residual.md G0.R2 (2026-06 review C3 — the AGE scale cliff
before Phase 2). Migrations 0001/0004 create every vertex/edge label but **zero**
indexes on their backing tables: AGE keeps all properties in one `agtype` column, so
every ``MERGE (n {id: ...})``, every box-scoped ``MATCH``, and every endpoint
traversal was a sequential scan of the label's heap — fine at Phase 1 volumes, a
cliff exactly where Phase 2 leans hardest (continuous per-mention candidate lookups
and ``SAME_AS``/``partOf`` component queries).

**What AGE's query plans actually do (verified by EXPLAIN, not assumed).** The gap
doc speculated a btree on ``agtype_access_operator(properties, '"id"')``. The real
plans never use that expression: a property-map filter like ``{id: 'x'}`` or
``{box: 'b'}`` compiles to the **agtype containment operator**
``properties @> '{"id": "x"}'::agtype``. A btree on the access-operator would exist
and never be chosen. The operator that *is* used (``@>``) is served by a **GIN index
on the whole ``properties`` column**, which therefore backs id-lookup, box-scoped
MATCH, and any ad-hoc property filter with a single index per vertex label — simpler
and more correct than one btree per property.

Edges are different: an edge ``MATCH``/``MERGE`` resolves its endpoints via the
vertex GIN, then joins/filters the edge table on the ``start_id``/``end_id`` graphid
columns (``r.start_id = a.id``). Those are plain graphid columns, so they take
**btree** indexes — the index the Phase 2 ``SAME_AS`` component walk and ``partOf``
roll-up traversal ride on. Edge *property* filters do not realize on any current path
(``merge_edge`` keys on endpoints + label, not properties; box-scoped edge queries
are Phase 4), so edge-property GIN is deferred to its consumer.

Bitemporal as-of range indexes (``valid_from``/``valid_to`` ``<``/``>`` filters) are
likewise deferred: no reader exists until Phase 5 supersession, and the as-of query
shape is undefined, so any range index would be unverifiable today. Containment/
equality on those fields already rides the vertex GIN. See the gap doc for the full
rationale.

This migration is graph-schema DDL with no autogenerate path (env.py `_include_object`
excludes schema ``iknos``), so there is nothing to mirror in `db/orm.py`.
"""

import re

from alembic import op

revision = "0007_age_label_indexes"
down_revision = "0006_actions_target_span_index"
branch_labels = None
depends_on = None


# Every vertex label created by 0001 + 0004. Each gets a GIN index on `properties`,
# which the planner uses for the `properties @> {...}` containment filter behind
# MERGE-by-id, box-scoped MATCH, and ad-hoc property lookups.
VERTEX_LABELS = (
    "Document",
    "Span",
    "Proposition",
    "Actor",
    "Object",
    "Fact",
    "DeductiveConclusion",
    "InductiveConclusion",
    "Hypothesis",
    "Box",
    "Mention",
    "Task",
)

# Every edge label created by 0001 + 0004. Each gets btree indexes on the graphid
# endpoint columns the planner joins on during traversal and MERGE existence checks.
EDGE_LABELS = (
    "EVIDENCED_BY",
    "INVOLVES",
    "DERIVED_FROM",
    "SUPPORTS",
    "REFUTES",
    "RELATES",
    "REFERS_TO",
    "SAME_AS",
    "directPartOf",
    "partOf",
    "DECOMPOSES_INTO",
    "ADDRESSES",
    "RELEVANT_TO",
)


def _slug(label: str) -> str:
    """Index-name-safe slug for an AGE label (lowercased, non-alnum -> '_').

    Label names are case-sensitive and mixed-case/underscored (``EVIDENCED_BY``,
    ``directPartOf``); the slug only names the index, so collapsing case is fine —
    ``directPartOf``/``partOf`` stay distinct (``directpartof``/``partof``).
    """
    return re.sub(r"[^0-9a-z]+", "_", label.lower()).strip("_")


def _vertex_index(label: str) -> str:
    return f"ix_{_slug(label)}_props"


def _edge_indexes(label: str) -> tuple[tuple[str, str], tuple[str, str]]:
    slug = _slug(label)
    return (f"ix_{slug}_start", "start_id"), (f"ix_{slug}_end", "end_id")


def upgrade() -> None:
    # The agtype GIN default opclass lives in ag_catalog; put it on the search_path
    # so `USING gin (properties)` resolves it. Index targets are fully qualified, so
    # they land in schema `iknos` regardless of the path.
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    for label in VERTEX_LABELS:
        op.execute(f'CREATE INDEX {_vertex_index(label)} ON iknos."{label}" USING gin (properties)')

    for label in EDGE_LABELS:
        (start_idx, start_col), (end_idx, end_col) = _edge_indexes(label)
        op.execute(f'CREATE INDEX {start_idx} ON iknos."{label}" ({start_col})')
        op.execute(f'CREATE INDEX {end_idx} ON iknos."{label}" ({end_col})')


def downgrade() -> None:
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    for label in EDGE_LABELS:
        (start_idx, _), (end_idx, _) = _edge_indexes(label)
        op.execute(f"DROP INDEX iknos.{start_idx}")
        op.execute(f"DROP INDEX iknos.{end_idx}")

    for label in VERTEX_LABELS:
        op.execute(f"DROP INDEX iknos.{_vertex_index(label)}")
