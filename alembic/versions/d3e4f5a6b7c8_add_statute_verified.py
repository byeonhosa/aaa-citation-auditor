"""Add statute_verified_count columns and statute_verification_cache table

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-03-13 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "audit_runs",
        sa.Column("statute_verified_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "telemetry_events",
        sa.Column("statute_verified_count", sa.Integer(), nullable=True),
    )
    op.create_table(
        "statute_verification_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("section_number", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("section_title", sa.Text(), nullable=True),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_statute_verification_cache_section_number",
        "statute_verification_cache",
        ["section_number"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_statute_verification_cache_section_number",
        table_name="statute_verification_cache",
    )
    op.drop_table("statute_verification_cache")
    op.drop_column("telemetry_events", "statute_verified_count")
    op.drop_column("audit_runs", "statute_verified_count")
