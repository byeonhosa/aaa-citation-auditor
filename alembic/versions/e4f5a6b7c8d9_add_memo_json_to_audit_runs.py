"""Add memo_json column to audit_runs

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-03-14 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add memo_json column to audit_runs for persisting AI risk memos."""
    op.add_column(
        "audit_runs",
        sa.Column("memo_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove memo_json column from audit_runs."""
    op.drop_column("audit_runs", "memo_json")
