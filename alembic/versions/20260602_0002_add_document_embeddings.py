"""add document embeddings

Revision ID: 0002_add_document_embeddings
Revises: 0001_initial
Create Date: 2026-06-02

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import pgvector.sqlalchemy

from alembic import op

revision = "0002_add_document_embeddings"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "document_embeddings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_content.document_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("span_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1024), nullable=False),
    )

def downgrade() -> None:
    op.drop_table("document_embeddings")
