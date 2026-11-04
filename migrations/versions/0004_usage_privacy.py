"""Usage analytics, feedback and player opt-outs (p4-08, p5-02, p5-03, p4-10).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            comment="ok | insufficient_data | error",
        ),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column(
            "message", sa.String(length=300), nullable=True, comment="why the call was not ok"
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="sources called, e.g. [opendota]",
        ),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("client_name", sa.String(length=100), nullable=True),
        sa.Column("client_version", sa.String(length=50), nullable=True),
        sa.Column("client_hash", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_usage_events")),
    )
    op.create_index("ix_usage_events_ts", "usage_events", ["ts"])
    op.create_index("ix_usage_events_tool_ts", "usage_events", ["tool", "ts"])
    op.create_table(
        "usage_feedback",
        sa.Column("feedback_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            comment="wrong_answer | feature_request",
        ),
        sa.Column("tool", sa.String(length=64), nullable=True),
        sa.Column(
            "call_id",
            sa.String(length=32),
            nullable=True,
            comment="the client's latest call of that tool, when it could be found",
        ),
        sa.Column("text", sa.String(length=2000), nullable=False),
        sa.Column("expected", sa.String(length=1000), nullable=True),
        sa.Column("client_name", sa.String(length=100), nullable=True),
        sa.Column("client_hash", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("feedback_id", name=op.f("pk_usage_feedback")),
    )
    op.create_index("ix_usage_feedback_ts", "usage_feedback", ["ts"])
    op.create_table(
        "player_opt_outs",
        sa.Column("account_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_player_opt_outs")),
    )


def downgrade() -> None:
    op.drop_table("player_opt_outs")
    op.drop_index("ix_usage_feedback_ts", table_name="usage_feedback")
    op.drop_table("usage_feedback")
    op.drop_index("ix_usage_events_tool_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_ts", table_name="usage_events")
    op.drop_table("usage_events")
