"""Period of the matches behind each patch's aggregates, for tool envelopes.

``aggregate_status.first_match_at`` / ``last_match_at`` give tools the date range and freshness
of what they read without scanning ``matches``. The aggregate job keeps them current; this
migration fills them once from the matches already aggregated.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aggregate_status",
        sa.Column(
            "first_match_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="start time of the oldest match in the draft aggregates",
        ),
    )
    op.add_column(
        "aggregate_status",
        sa.Column(
            "last_match_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="start time of the newest match in the draft aggregates",
        ),
    )
    op.execute(
        """
        INSERT INTO aggregate_status (patch_id, first_match_at, last_match_at)
        SELECT patch_id, min(start_time), max(start_time)
        FROM matches
        WHERE aggregated_at IS NOT NULL
        GROUP BY patch_id
        ON CONFLICT (patch_id) DO UPDATE
        SET first_match_at = excluded.first_match_at, last_match_at = excluded.last_match_at
        """
    )


def downgrade() -> None:
    op.drop_column("aggregate_status", "last_match_at")
    op.drop_column("aggregate_status", "first_match_at")
