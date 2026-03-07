"""add disambiguation columns and citation_resolution_cache table

Revision ID: b1c2d3e4f5a6
Revises: a6175d945c95
Create Date: 2026-03-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a6175d945c95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("citation_results", schema=None) as batch_op:
        batch_op.add_column(sa.Column("candidate_cluster_ids", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("candidate_metadata", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("selected_cluster_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("resolution_method", sa.String(32), nullable=True))

    op.create_table(
        "citation_resolution_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("normalized_cite", sa.String(512), nullable=False),
        sa.Column("selected_cluster_id", sa.Integer(), nullable=False),
        sa.Column("case_name", sa.String(512), nullable=True),
        sa.Column("court", sa.String(128), nullable=True),
        sa.Column("date_filed", sa.String(32), nullable=True),
        sa.Column("resolution_method", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_citation_resolution_cache_normalized_cite",
        "citation_resolution_cache",
        ["normalized_cite"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_citation_resolution_cache_normalized_cite",
        table_name="citation_resolution_cache",
    )
    op.drop_table("citation_resolution_cache")

    with op.batch_alter_table("citation_results", schema=None) as batch_op:
        batch_op.drop_column("resolution_method")
        batch_op.drop_column("selected_cluster_id")
        batch_op.drop_column("candidate_metadata")
        batch_op.drop_column("candidate_cluster_ids")
