"""actions extract-fact index: back the §10.2 audit reach-back (G2.7)

Revision ID: 0008_actions_extract_fact_index
Revises: 0007_age_label_indexes
Create Date: 2026-06-11

Touches AGE graph: no (relational only)

G2.7's auditability reach-back (provenance.audit::producing_action / fact_provenance) finds
the Action that produced a Fact by filtering actions on outputs->>'fact' for actor='extractor'
and taking the newest row. Without an index that is a sequential scan of the append-only,
unbounded action log per Fact — and the audit / Phase-7 review path queries it per Fact. This
mirrors the G1.7 idempotency index (0006): functional on the JSONB-nested id, partial on the
single actor that ever writes that key, with a trailing (timestamp DESC) leg serving the
ORDER BY ... LIMIT 1 directly. Mirrored in iknos.db.orm.Action.__table_args__ so the
autogenerate-drift gate stays clean.
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_actions_extract_fact_index"
down_revision = "0007_age_label_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_actions_extract_fact",
        "actions",
        [sa.text("(outputs->>'fact')"), sa.text("timestamp DESC")],
        postgresql_where=sa.text("actor = 'extractor'"),
    )


def downgrade() -> None:
    op.drop_index("ix_actions_extract_fact", table_name="actions")
