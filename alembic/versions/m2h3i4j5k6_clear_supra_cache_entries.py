"""Clear stale supra cache entries

Removes any CitationResolutionCache rows whose normalized_cite is a bare
supra token (e.g. "supra," or "supra.").  These were incorrectly cached
because all supra citations shared the same normalized text from eyecite.

Revision ID: m2h3i4j5k6
Revises: l1g2h3i4j5
Create Date: 2026-03-21 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "m2h3i4j5k6"
down_revision: Union[str, None] = "a88a291f2ec9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM citation_resolution_cache"
            " WHERE normalized_cite LIKE 'supra%'"
            " AND LENGTH(TRIM(normalized_cite)) <= 7"
        )
    )


def downgrade() -> None:
    # Deleted rows cannot be recovered automatically.
    pass
