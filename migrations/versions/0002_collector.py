"""Collector state, draft-level and histogram aggregates, incremental aggregation markers.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Draft-only rows from publicMatches have no player stats until enrichment fills them in.
NULLABLE_PLAYER_STATS = ("kills", "deaths", "assists", "gpm", "xpm", "last_hits", "denies")


def upgrade() -> None:
    op.create_table(
        "collector_api_usage",
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("window", sa.String(length=8), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "source", "window", "window_start", name=op.f("pk_collector_api_usage")
        ),
    )
    op.create_table(
        "collector_cursors",
        sa.Column("stream", sa.String(length=32), nullable=False),
        sa.Column(
            "newest_match_id",
            sa.BigInteger(),
            nullable=False,
            comment="every settled match up to this id has been read",
        ),
        sa.Column(
            "newest_start_time",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="start time at that id: how far behind the stream is",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("stream", name=op.f("pk_collector_cursors")),
    )
    op.create_table(
        "collector_gaps",
        sa.Column("gap_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stream", sa.String(length=32), nullable=False),
        sa.Column("low_match_id", sa.BigInteger(), nullable=False),
        sa.Column("high_match_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "low_match_id < high_match_id", name=op.f("ck_collector_gaps_ordered_bounds")
        ),
        sa.PrimaryKeyConstraint("gap_id", name=op.f("pk_collector_gaps")),
    )
    op.create_index(
        "ix_collector_gaps_stream_open",
        "collector_gaps",
        ["stream"],
        unique=False,
        postgresql_where=sa.text("filled_at IS NULL"),
    )
    op.create_table(
        "collector_job_runs",
        sa.Column("run_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="running | ok | skipped | failed",
        ),
        sa.Column("api_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_collector_job_runs")),
    )
    op.create_index(
        "ix_collector_job_runs_job_started_at",
        "collector_job_runs",
        ["job", "started_at"],
        unique=False,
    )
    op.create_table(
        "hero_stats_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("rank_bracket", sa.SmallInteger(), nullable=False),
        sa.Column("hero_id", sa.SmallInteger(), nullable=False),
        sa.Column("picks", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("bans", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "rank_bracket BETWEEN 0 AND 8", name=op.f("ck_hero_stats_daily_rank_bracket_range")
        ),
        sa.PrimaryKeyConstraint(
            "day", "scope", "rank_bracket", "hero_id", name=op.f("pk_hero_stats_daily")
        ),
    )
    op.create_table(
        "reference_snapshots",
        sa.Column("resource", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("resource", "sha256", name=op.f("pk_reference_snapshots")),
    )
    op.create_table(
        "aggregate_status",
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stale_reason", sa.String(length=200), nullable=True),
        sa.Column("stale_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["patch_id"], ["patches.patch_id"], name=op.f("fk_aggregate_status_patch_id_patches")
        ),
        sa.PrimaryKeyConstraint("patch_id", name=op.f("pk_aggregate_status")),
    )
    op.create_table(
        "hero_draft_aggregates",
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("rank_bracket", sa.SmallInteger(), nullable=False),
        sa.Column("hero_id", sa.SmallInteger(), nullable=False),
        sa.Column("bans", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rank_bracket BETWEEN 0 AND 8", name=op.f("ck_hero_draft_aggregates_rank_bracket_range")
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["patches.patch_id"],
            name=op.f("fk_hero_draft_aggregates_patch_id_patches"),
        ),
        sa.PrimaryKeyConstraint(
            "patch_id", "rank_bracket", "hero_id", name=op.f("pk_hero_draft_aggregates")
        ),
    )
    op.create_table(
        "hero_stat_histograms",
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("rank_bracket", sa.SmallInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("hero_id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "stat",
            sa.Enum(
                "gpm",
                "xpm",
                "net_worth",
                name="histogram_stat",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("bucket", sa.Integer(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 5", name=op.f("ck_hero_stat_histograms_position_range")
        ),
        sa.CheckConstraint(
            "rank_bracket BETWEEN 0 AND 8", name=op.f("ck_hero_stat_histograms_rank_bracket_range")
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["patches.patch_id"],
            name=op.f("fk_hero_stat_histograms_patch_id_patches"),
        ),
        sa.PrimaryKeyConstraint(
            "patch_id",
            "rank_bracket",
            "position",
            "hero_id",
            "stat",
            "bucket",
            name=op.f("pk_hero_stat_histograms"),
        ),
    )
    for column in NULLABLE_PLAYER_STATS:
        op.alter_column("match_players", column, existing_type=sa.SmallInteger(), nullable=True)
    op.alter_column(
        "match_players",
        "items",
        existing_type=postgresql.ARRAY(sa.INTEGER()),
        nullable=True,
        existing_comment="final inventory item ids, slots 0-5; 0 = empty",
    )
    op.add_column(
        "matches",
        sa.Column(
            "has_details",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="per-player stats and positions are present, not just the draft",
        ),
    )
    op.add_column(
        "matches",
        sa.Column(
            "detail_attempts",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="enrichment lookups that found nothing",
        ),
    )
    op.add_column(
        "matches",
        sa.Column(
            "banned_hero_ids",
            postgresql.ARRAY(sa.Integer()),
            nullable=True,
            comment="NULL = bans unknown (public match bans are hidden by the sources)",
        ),
    )
    op.add_column(
        "matches",
        sa.Column(
            "aggregated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="draft-level aggregates include this match",
        ),
    )
    op.add_column(
        "matches",
        sa.Column(
            "details_aggregated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="detail-level aggregates include this match",
        ),
    )
    op.create_index(
        "ix_matches_pending_aggregation",
        "matches",
        ["patch_id"],
        unique=False,
        postgresql_where=sa.text("aggregated_at IS NULL"),
    )
    op.create_index(
        "ix_matches_pending_details_aggregation",
        "matches",
        ["patch_id"],
        unique=False,
        postgresql_where=sa.text("has_details AND details_aggregated_at IS NULL"),
    )
    op.add_column(
        "patch_bracket_totals",
        sa.Column("detailed_games", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "patch_bracket_totals",
        sa.Column("ban_sample_games", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    # Draft-only rows cannot satisfy the restored NOT NULL constraints; they are dropped.
    op.execute("DELETE FROM match_players WHERE gpm IS NULL OR items IS NULL")
    op.execute("DELETE FROM matches WHERE NOT has_details")
    op.drop_column("patch_bracket_totals", "ban_sample_games")
    op.drop_column("patch_bracket_totals", "detailed_games")
    op.drop_index(
        "ix_matches_pending_details_aggregation",
        table_name="matches",
        postgresql_where=sa.text("has_details AND details_aggregated_at IS NULL"),
    )
    op.drop_index(
        "ix_matches_pending_aggregation",
        table_name="matches",
        postgresql_where=sa.text("aggregated_at IS NULL"),
    )
    op.drop_column("matches", "details_aggregated_at")
    op.drop_column("matches", "aggregated_at")
    op.drop_column("matches", "banned_hero_ids")
    op.drop_column("matches", "detail_attempts")
    op.drop_column("matches", "has_details")
    op.alter_column(
        "match_players",
        "items",
        existing_type=postgresql.ARRAY(sa.INTEGER()),
        nullable=False,
        existing_comment="final inventory item ids, slots 0-5; 0 = empty",
    )
    for column in NULLABLE_PLAYER_STATS:
        op.alter_column("match_players", column, existing_type=sa.SmallInteger(), nullable=False)
    op.drop_table("hero_stat_histograms")
    op.drop_table("hero_draft_aggregates")
    op.drop_table("aggregate_status")
    op.drop_table("reference_snapshots")
    op.drop_table("hero_stats_daily")
    op.drop_index("ix_collector_job_runs_job_started_at", table_name="collector_job_runs")
    op.drop_table("collector_job_runs")
    op.drop_index(
        "ix_collector_gaps_stream_open",
        table_name="collector_gaps",
        postgresql_where=sa.text("filled_at IS NULL"),
    )
    op.drop_table("collector_gaps")
    op.drop_table("collector_cursors")
    op.drop_table("collector_api_usage")
