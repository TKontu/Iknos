"""schema revision: identity, part-whole, intentional labels

Revision ID: 0004_schema_revision
Revises: 0003_proposition_layer
Create Date: 2026-06-09

Touches AGE graph: yes

Widens the fixed epistemic schema (architecture.md §10) per the revised plan,
closing gaps G0.2 (new vlabels) and G0.3 (new elabels) from
docs/gap_phase_0_foundations.md. Labels only — AGE is schema-less for vertex/edge
properties, so the property contracts (scored Annotations + bitemporal on
REFERS_TO/SAME_AS, meronymy-type tag on directPartOf/partOf, etc.) are enforced
by the Pydantic projections that land in later phases, not by this DDL.

New vlabels:
- Mention (§3.1)        — textual mention bound to a canonical entity.
- Task    (§11.2)       — intentional layer; answer_state is *answered*, not
                          epistemically adjudicated.

New elabels:
- REFERS_TO    (§3.1)   — Mention -> Actor/Object, scored/defeasible.
- SAME_AS      (§5.2)   — Actor/Object identity; the connected component is the
                          canonical entity.
- directPartOf (§14)    — each direct decomposition step (intransitive).
- partOf       (§14)    — transitive closure of directPartOf.
- DECOMPOSES_INTO (§11.2) — Task -> sub-Task.
- ADDRESSES    (§11.2)
- RELEVANT_TO  (§11.2)
"""

from alembic import op

revision = "0004_schema_revision"
down_revision = "0003_proposition_layer"
branch_labels = None
depends_on = None


NEW_VERTEX_LABELS = (
    "Mention",
    "Task",
)

NEW_EDGE_LABELS = (
    "REFERS_TO",
    "SAME_AS",
    "directPartOf",
    "partOf",
    "DECOMPOSES_INTO",
    "ADDRESSES",
    "RELEVANT_TO",
)


def upgrade() -> None:
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    for label in NEW_VERTEX_LABELS:
        op.execute(f"SELECT create_vlabel('iknos', '{label}')")

    for label in NEW_EDGE_LABELS:
        op.execute(f"SELECT create_elabel('iknos', '{label}')")


def downgrade() -> None:
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    # drop_label removes the label's backing table; safe only because no
    # production graph carries these labels yet (pre-implementation schema).
    for label in NEW_EDGE_LABELS:
        op.execute(f"SELECT drop_label('iknos', '{label}')")

    for label in NEW_VERTEX_LABELS:
        op.execute(f"SELECT drop_label('iknos', '{label}')")
