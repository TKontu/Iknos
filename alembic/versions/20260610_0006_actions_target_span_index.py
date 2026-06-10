"""actions target_span index: back the propositionizer idempotency lookup (G1.7)

Revision ID: 0006_actions_target_span_index
Revises: 0005_span_embedding_unique
Create Date: 2026-06-10

Touches AGE graph: no (relational only)

G1.7 makes the propositionizer's idempotency check version-aware: per span it reads the
content_hash of that span's most recent extract Action (core/proposition.py::_extracted_hash)
and decides no-op vs re-extract vs StaleExtractionError. That lookup filters on the JSONB
expression inputs->>'target_span' for actor='propositionizer' and takes the newest row — a
sequential scan without this index (the pre-G1.7 lookup was unindexed too). As the actions
audit log grows unbounded, an index keeps ingest idempotency O(log n) instead of O(n) per span.

Functional + partial: the span id lives in JSONB inputs, and only propositionizer rows are ever
looked up this way. The trailing (timestamp DESC) leg serves the ORDER BY ... LIMIT 1 directly.
Mirrored in iknos.db.orm.Action.__table_args__ so the autogenerate-drift gate stays clean.
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_actions_target_span_index"
down_revision = "0005_span_embedding_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_actions_extract_target_span",
        "actions",
        [sa.text("(inputs->>'target_span')"), sa.text("timestamp DESC")],
        postgresql_where=sa.text("actor = 'propositionizer'"),
    )


def downgrade() -> None:
    op.drop_index("ix_actions_extract_target_span", table_name="actions")
