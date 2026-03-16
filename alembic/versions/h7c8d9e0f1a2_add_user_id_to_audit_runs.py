"""Add user_id to audit_runs

Revision ID: h7c8d9e0f1a2
Revises: g6b7c8d9e0f1
Create Date: 2026-03-15 00:00:01.000000


"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h7c8d9e0f1a2"
down_revision: Union[str, None] = "g6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use batch mode for SQLite compatibility (SQLite does not support
    # ALTER TABLE ADD COLUMN with a foreign key constraint directly).
    with op.batch_alter_table("audit_runs") as batch_op:
        batch_op.add_column(
            sa.Column("user_id", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_runs") as batch_op:
        batch_op.drop_column("user_id")
