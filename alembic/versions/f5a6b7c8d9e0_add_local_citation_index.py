"""Add local_citation_index table

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-03-14 00:00:00.000000


"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create local_citation_index table for bulk CourtListener data."""
    op.create_table(
        "local_citation_index",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("normalized_cite", sa.String(512), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("case_name", sa.String(512), nullable=True),
        sa.Column("court_id", sa.String(128), nullable=True),
        sa.Column("date_filed", sa.String(32), nullable=True),
        sa.Column("reporter", sa.String(64), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.Column("page", sa.String(32), nullable=True),
        sa.Column("source", sa.String(64), nullable=False, server_default="courtlistener_bulk"),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_local_citation_index_normalized_cite",
        "local_citation_index",
        ["normalized_cite"],
        unique=True,
    )
    op.create_index(
        "ix_local_citation_index_cluster_id",
        "local_citation_index",
        ["cluster_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop local_citation_index table."""
    op.drop_index("ix_local_citation_index_cluster_id", table_name="local_citation_index")
    op.drop_index("ix_local_citation_index_normalized_cite", table_name="local_citation_index")
    op.drop_table("local_citation_index")
