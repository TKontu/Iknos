"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-01

Touches AGE graph: yes

Creates extensions (pgcrypto, vector, age), the single AGE graph 'iknos',
pre-creates all §10 vertex/edge labels, and the relational tables
(document_content, actions).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


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
)

EDGE_LABELS = (
    "EVIDENCED_BY",
    "INVOLVES",
    "DERIVED_FROM",
    "SUPPORTS",
    "REFUTES",
    "RELATES",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS age")
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    op.execute("SELECT create_graph('iknos')")

    for label in VERTEX_LABELS:
        op.execute(f"SELECT create_vlabel('iknos', '{label}')")

    for label in EDGE_LABELS:
        op.execute(f"SELECT create_elabel('iknos', '{label}')")

    # AGE prepended ag_catalog to the search_path above. Reset it so the
    # relational tables/indexes below are created in `public` — otherwise they
    # land in ag_catalog and the downgrade (which drops from public) cannot find
    # them. Keep the graph DDL above and the relational DDL below schema-separated.
    op.execute("SET search_path = public")

    op.create_table(
        "document_content",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "actions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column(
            "inputs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "outputs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("sampling", postgresql.JSONB(), nullable=True),
        sa.Column("raw_judgment", sa.Text(), nullable=True),
        sa.Column("calibration", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_actions_timestamp", "actions", ["timestamp"])
    op.create_index("ix_actions_actor_type", "actions", ["actor", "action_type"])


def downgrade() -> None:
    # Relational objects live in `public` (see the search_path reset in upgrade).
    # Pin it explicitly so these drops resolve regardless of the role's default.
    op.execute("SET search_path = public")
    op.drop_index("ix_actions_actor_type", table_name="actions")
    op.drop_index("ix_actions_timestamp", table_name="actions")
    op.drop_table("actions")
    op.drop_table("document_content")

    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')
    op.execute("SELECT drop_graph('iknos', true)")
    # Extensions are intentionally left in place — dropping them could affect
    # other schemas in the same database.
