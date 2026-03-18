"""Add trust tier columns to citation_resolution_cache

Revision ID: j9e0f1a2b3c4
Revises: i8d9e0f1a2b3
Create Date: 2026-03-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "j9e0f1a2b3c4"
down_revision: Union[str, None] = "i8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("citation_resolution_cache") as batch_op:
        batch_op.add_column(
            sa.Column("trust_tier", sa.String(32), nullable=False, server_default="algorithmic")
        )
        batch_op.add_column(sa.Column("cache_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("last_reverified_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("disputed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("unique_user_count", sa.Integer(), nullable=False, server_default="1")
        )

    # Backfill trust_tier based on resolution_method
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE citation_resolution_cache SET trust_tier =
              CASE resolution_method
                WHEN 'direct' THEN 'authoritative'
                WHEN 'local_index' THEN 'authoritative'
                WHEN 'user' THEN 'user_submitted'
                ELSE 'algorithmic'
              END
            WHERE trust_tier = 'algorithmic'
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("citation_resolution_cache") as batch_op:
        batch_op.drop_column("unique_user_count")
        batch_op.drop_column("disputed")
        batch_op.drop_column("last_reverified_at")
        batch_op.drop_column("cache_user_id")
        batch_op.drop_column("trust_tier")
