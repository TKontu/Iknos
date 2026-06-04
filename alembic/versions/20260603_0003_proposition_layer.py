"""proposition layer

Revision ID: 0003_proposition_layer
Revises: 0002_add_document_embeddings
Create Date: 2026-06-03

Adds the dense (proposition_embeddings) and sparse (proposition_lexical_index)
indexes for the proposition layer. No AGE DDL — the Proposition vertex label is
already created in 0001. The lexical index uses a GIN index on the tsvector; the
tsvector itself is built with the `simple` config by the application (unstemmed,
no stop-words) so codes/acronyms survive verbatim for exact recall.
"""

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_proposition_layer"
down_revision = "0002_add_document_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposition_embeddings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("proposition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_content.document_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1024), nullable=False),
    )
    op.create_index(
        "ix_proposition_embeddings_proposition_id",
        "proposition_embeddings",
        ["proposition_id"],
    )
    op.create_index(
        "ix_proposition_embeddings_document_id",
        "proposition_embeddings",
        ["document_id"],
    )

    op.create_table(
        "proposition_lexical_index",
        sa.Column("proposition_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_content.document_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lexemes", postgresql.TSVECTOR(), nullable=False),
    )
    op.create_index(
        "ix_proposition_lexical_index_document_id",
        "proposition_lexical_index",
        ["document_id"],
    )
    op.create_index(
        "ix_proposition_lexical_lexemes",
        "proposition_lexical_index",
        ["lexemes"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_proposition_lexical_lexemes", table_name="proposition_lexical_index")
    op.drop_index(
        "ix_proposition_lexical_index_document_id", table_name="proposition_lexical_index"
    )
    op.drop_table("proposition_lexical_index")
    op.drop_index("ix_proposition_embeddings_document_id", table_name="proposition_embeddings")
    op.drop_index("ix_proposition_embeddings_proposition_id", table_name="proposition_embeddings")
    op.drop_table("proposition_embeddings")
