"""Assign NULL-owner audit runs to the first registered user

One-time data migration: audit runs created before user accounts were
introduced have user_id = NULL.  If a user with id = 1 exists, all such
orphaned runs are re-assigned to that user so they appear in their
history and are no longer visible to other accounts.

Revision ID: i8d9e0f1a2b3
Revises: h7c8d9e0f1a2
Create Date: 2026-03-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "i8d9e0f1a2b3"
down_revision: Union[str, None] = "h7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Only reassign if user id=1 actually exists.
    row = conn.execute(sa.text("SELECT id FROM users WHERE id = 1 LIMIT 1")).fetchone()
    if row is None:
        return

    result = conn.execute(sa.text("UPDATE audit_runs SET user_id = 1 WHERE user_id IS NULL"))
    updated = result.rowcount
    if updated:
        import logging

        logging.getLogger(__name__).info(
            "Data migration: assigned %d NULL-owner audit run(s) to user id=1", updated
        )


def downgrade() -> None:
    # Reversing a data migration would lose information about which runs were
    # originally ownerless; leave them assigned rather than nulling them out.
    pass
