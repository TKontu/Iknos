"""ANCHORS_TO edge label + endpoint indexes: entity-linking / taxonomy anchoring (G2.8)

Revision ID: 0011_anchors_to_label
Revises: 0010_actions_ingest_indexes
Create Date: 2026-06-11

Touches AGE graph: yes (creates a new edge label + indexes on its backing table)

G2.8 introduces **entity-linking / taxonomy anchoring** (architecture.md §5.2, §9, §14):
a case box's ``Actor``/``Object`` entities are linked to the active domain pack(s)'
taxonomy ``Object`` nodes, recording each link as a scored, directed ``ANCHORS_TO`` edge
(case entity → taxonomy node — the direction encodes *anchor canonicalizes*: the taxonomy
node is the authoritative identity, §5.2/§14).

``ANCHORS_TO`` is a **new edge label** — migrations 0001/0004 created every other
vertex/edge label but not this one, because anchoring had no operator until G2.8. This
migration creates the elabel (``create_elabel`` — the 0004 pattern) and the two endpoint
btree indexes the 0007 migration adds for every edge label: an edge ``MATCH``/``MERGE``
resolves its endpoints via the vertex GIN, then joins/filters the edge table on the
``start_id``/``end_id`` graphid columns. The anchoring reads (``EntityLinker.coverage`` /
``anchored_targets``) and the per-entity ``merge_edge`` existence check traverse those
columns per case entity, so without the btrees they would sequentially scan the
``ANCHORS_TO`` heap — the exact Phase-2 continuous-lookup cliff 0007 closed for the other
labels. Edge *property* filters (``r.state``) do not realize on an index on any current
path (``merge_edge`` keys on endpoints + label, and the state filter rides the small
per-entity edge fan-out after the endpoint join), so edge-property GIN is deferred to a
consumer that needs it, consistent with 0007.

This migration is graph-schema DDL with no autogenerate path (env.py ``_include_object``
excludes schema ``iknos``), so there is nothing to mirror in ``db/orm.py`` (cf. 0007).
"""

import re

from alembic import op

revision = "0011_anchors_to_label"
down_revision = "0010_actions_ingest_indexes"
branch_labels = None
depends_on = None


# The new edge label this migration adds, with its endpoint btree indexes (the 0007
# edge-index pattern). Kept as a tuple so widening to more labels stays one loop.
NEW_EDGE_LABELS = ("ANCHORS_TO",)


def _slug(label: str) -> str:
    """Index-name-safe slug for an AGE label (lowercased, non-alnum -> '_'); cf. 0007."""
    return re.sub(r"[^0-9a-z]+", "_", label.lower()).strip("_")


def _edge_indexes(label: str) -> tuple[tuple[str, str], tuple[str, str]]:
    slug = _slug(label)
    return (f"ix_{slug}_start", "start_id"), (f"ix_{slug}_end", "end_id")


def upgrade() -> None:
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    for label in NEW_EDGE_LABELS:
        op.execute(f"SELECT create_elabel('iknos', '{label}')")
        (start_idx, start_col), (end_idx, end_col) = _edge_indexes(label)
        op.execute(f'CREATE INDEX {start_idx} ON iknos."{label}" ({start_col})')
        op.execute(f'CREATE INDEX {end_idx} ON iknos."{label}" ({end_col})')


def downgrade() -> None:
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    for label in NEW_EDGE_LABELS:
        (start_idx, _), (end_idx, _) = _edge_indexes(label)
        op.execute(f"DROP INDEX iknos.{start_idx}")
        op.execute(f"DROP INDEX iknos.{end_idx}")
        # drop_label removes the label's backing table; safe only because no production
        # graph carries ANCHORS_TO yet (pre-implementation schema), cf. 0004 downgrade.
        op.execute(f"SELECT drop_label('iknos', '{label}')")
