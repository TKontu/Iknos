"""actions parse/segment indexes: back the parse + segment idempotency lookups (G1.17 R4)

Revision ID: 0010_actions_ingest_indexes
Revises: 0009_actions_extract_fact_index
Create Date: 2026-06-11

Touches AGE graph: no (relational only)

Migration 0006 added a partial functional index for the *propositionizer* idempotency lookup
(``inputs->>'target_span'`` where ``actor='propositionizer'``). The parse and segmentation stages
run the same shape of lookup — ``core/ingest.py::_parsed_hash`` and ``_segmented_hash`` each read
the newest Action for a document via ``inputs->>'document_id'`` filtered by actor
(``'parser'`` / ``'segmenter'``), ``ORDER BY timestamp DESC LIMIT 1`` — but had no backing index,
so every ingest decision sequentially scanned the append-only, ever-growing ``actions`` log.

This adds the two mirrors of 0006: functional on ``(inputs->>'document_id')`` + a trailing
``timestamp DESC`` leg that serves the ``ORDER BY ... LIMIT 1`` directly, partial on the one actor
each is ever queried under. Note: ``actions`` is append-only and on the hot path of every ingest
decision; partitioning the table is deferred until volume warrants it — until then these partial
indexes keep the lookups O(log n).

The revision id is kept short: ``alembic_version.version_num`` is ``varchar(32)``.
Mirrored in iknos.db.orm.Action.__table_args__ so the autogenerate-drift gate stays clean.
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_actions_ingest_indexes"
down_revision = "0009_actions_extract_fact_index"
branch_labels = None
depends_on = None

# (index name, actor) — the document-keyed newest-Action lookup each ingest stage runs.
_INDEXES = (
    ("ix_actions_parse_document_id", "parser"),
    ("ix_actions_segment_document_id", "segmenter"),
)


def upgrade() -> None:
    for name, actor in _INDEXES:
        op.create_index(
            name,
            "actions",
            [sa.text("(inputs->>'document_id')"), sa.text("timestamp DESC")],
            postgresql_where=sa.text(f"actor = '{actor}'"),
        )


def downgrade() -> None:
    for name, _actor in _INDEXES:
        op.drop_index(name, table_name="actions")
