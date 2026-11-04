"""Developer API keys and the channel of each usage event (p5-06).

``api_keys`` holds only an HMAC-SHA256 of each key (``dota_insights.api.keys``). Usage events
get ``channel``: ``mcp`` for MCP tool calls (every existing row), ``api`` for ``/api/v1`` calls.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "prefix",
            sa.String(length=12),
            nullable=False,
            comment="first characters, to recognise a key",
        ),
        sa.Column(
            "name", sa.String(length=100), nullable=False, comment="who or what the key is for"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "per_minute",
            sa.Integer(),
            nullable=True,
            comment="calls a minute; null: settings.api_key_per_minute",
        ),
        sa.Column(
            "per_day",
            sa.Integer(),
            nullable=True,
            comment="calls a day; null: settings.api_key_per_day",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.add_column(
        "usage_events",
        sa.Column(
            "channel",
            sa.String(length=8),
            server_default=sa.text("'mcp'"),
            nullable=False,
            comment="mcp | api (the /api/v1 REST routes)",
        ),
    )


def downgrade() -> None:
    op.drop_column("usage_events", "channel")
    op.drop_table("api_keys")
