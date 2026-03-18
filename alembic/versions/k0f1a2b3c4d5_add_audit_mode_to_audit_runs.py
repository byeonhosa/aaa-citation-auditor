"""Add audit_mode column to audit_runs

Revision ID: k0f1a2b3c4d5
Revises: j9e0f1a2b3c4
Create Date: 2026-03-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "k0f1a2b3c4d5"
down_revision: Union[str, None] = "j9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("audit_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "audit_mode",
                sa.String(32),
                nullable=False,
                server_default="self_review",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_runs") as batch_op:
        batch_op.drop_column("audit_mode")
