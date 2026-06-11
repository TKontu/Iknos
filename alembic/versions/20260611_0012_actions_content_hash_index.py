"""actions content_hash index: back the cross-doc extraction reuse lookup (G1.7b)

Revision ID: 0012_actions_content_hash_index
Revises: 0011_anchors_to_label
Create Date: 2026-06-11

Touches AGE graph: no (relational only)

G1.7b ("extract once" across documents) lets a never-extracted span whose content_hash matches a
*previously committed* extraction replay that extraction's propositions instead of re-running the
LLM (core/reuse.py::find_reusable_extraction). That lookup filters on the JSONB expression
inputs->>'content_hash' for actor='propositionizer' and takes the newest row — a sequential scan
over the append-only, ever-growing actions log without an index, run once per never-extracted span
on every ingest.

Same functional + partial shape as 0006's target_span index (the other half of the propositionizer
idempotency machinery): functional on inputs->>'content_hash' (the hash lives in JSONB inputs),
partial on the one actor ever queried this way, with a trailing (timestamp DESC) leg that serves
the ORDER BY ... LIMIT 1 directly. Keeps the reuse decision O(log n).

The revision id is kept short: alembic_version.version_num is varchar(32).
Mirrored in iknos.db.orm.Action.__table_args__ so the autogenerate-drift gate stays clean.
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_actions_content_hash_index"
down_revision = "0011_anchors_to_label"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_actions_extract_content_hash",
        "actions",
        [sa.text("(inputs->>'content_hash')"), sa.text("timestamp DESC")],
        postgresql_where=sa.text("actor = 'propositionizer'"),
    )


def downgrade() -> None:
    op.drop_index("ix_actions_extract_content_hash", table_name="actions")
