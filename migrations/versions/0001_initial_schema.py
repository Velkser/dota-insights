"""Initial schema: patches, partitioned per-match tables, per-patch aggregates.

Revision ID: 0001
Revises:
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Creates the monthly partition of every per-match table that holds ``ts`` (months are UTC).
# Idempotent; the collector calls it before inserting the first match of a month.
ENSURE_MONTH_PARTITIONS = """
CREATE OR REPLACE FUNCTION ensure_month_partitions(ts timestamptz) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    parent text;
    -- Month arithmetic on UTC wall-clock timestamps: timestamptz + interval would follow the
    -- session time zone and give non-UTC month bounds.
    month_utc timestamp := date_trunc('month', ts AT TIME ZONE 'UTC');
    lower_bound timestamptz := month_utc AT TIME ZONE 'UTC';
    upper_bound timestamptz := (month_utc + interval '1 month') AT TIME ZONE 'UTC';
BEGIN
    FOREACH parent IN ARRAY ARRAY['matches', 'match_players', 'player_timeseries', 'match_events']
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
            parent || '_' || to_char(month_utc, 'YYYY_MM'),
            parent, lower_bound, upper_bound
        );
    END LOOP;
END
$$
"""

# Fixed, not relative to now(), so the migration renders the same SQL whenever it runs.
PRECREATED_MONTHS = """
SELECT ensure_month_partitions(month)
FROM generate_series(
    timestamptz '2026-01-01 00:00:00+00', timestamptz '2027-12-01 00:00:00+00', interval '1 month'
) AS month
"""


def upgrade() -> None:
    op.create_table(
        "patches",
        sa.Column("patch_id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=16), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("patch_id", name=op.f("pk_patches")),
        sa.UniqueConstraint("name", name=op.f("uq_patches_name")),
        sa.UniqueConstraint("released_at", name=op.f("uq_patches_released_at")),
    )
    op.create_table(
        "hero_aggregates",
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("rank_bracket", sa.SmallInteger(), nullable=False),
        sa.Column(
            "position", sa.SmallInteger(), nullable=False, comment="1-5; 0 = position unknown"
        ),
        sa.Column("hero_id", sa.SmallInteger(), nullable=False),
        sa.Column("sum_kills", sa.BigInteger(), nullable=False),
        sa.Column("sum_deaths", sa.BigInteger(), nullable=False),
        sa.Column("sum_assists", sa.BigInteger(), nullable=False),
        sa.Column("sum_gpm", sa.BigInteger(), nullable=False),
        sa.Column("sum_xpm", sa.BigInteger(), nullable=False),
        sa.Column("sum_last_hits", sa.BigInteger(), nullable=False),
        sa.Column("sum_net_worth", sa.BigInteger(), nullable=False),
        sa.Column("sum_hero_damage", sa.BigInteger(), nullable=False),
        sa.Column("sum_tower_damage", sa.BigInteger(), nullable=False),
        sa.Column("sum_healing", sa.BigInteger(), nullable=False),
        sa.Column("sum_duration", sa.BigInteger(), nullable=False, comment="seconds"),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 5", name=op.f("ck_hero_aggregates_position_range")
        ),
        sa.CheckConstraint(
            "rank_bracket BETWEEN 0 AND 8", name=op.f("ck_hero_aggregates_rank_bracket_range")
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"], ["patches.patch_id"], name=op.f("fk_hero_aggregates_patch_id_patches")
        ),
        sa.PrimaryKeyConstraint(
            "patch_id", "rank_bracket", "position", "hero_id", name=op.f("pk_hero_aggregates")
        ),
    )
    op.create_index(
        "ix_hero_aggregates_hero_patch", "hero_aggregates", ["hero_id", "patch_id"], unique=False
    )
    op.create_table(
        "hero_matchup_aggregates",
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("rank_bracket", sa.SmallInteger(), nullable=False),
        sa.Column("hero_id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "relation",
            sa.Enum(
                "with",
                "vs",
                name="matchup_relation",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("other_hero_id", sa.SmallInteger(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "hero_id <> other_hero_id", name=op.f("ck_hero_matchup_aggregates_distinct_heroes")
        ),
        sa.CheckConstraint(
            "rank_bracket BETWEEN 0 AND 8",
            name=op.f("ck_hero_matchup_aggregates_rank_bracket_range"),
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["patches.patch_id"],
            name=op.f("fk_hero_matchup_aggregates_patch_id_patches"),
        ),
        sa.PrimaryKeyConstraint(
            "patch_id",
            "rank_bracket",
            "hero_id",
            "relation",
            "other_hero_id",
            name=op.f("pk_hero_matchup_aggregates"),
        ),
    )
    op.create_table(
        "item_timing_aggregates",
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("rank_bracket", sa.SmallInteger(), nullable=False),
        sa.Column("hero_id", sa.SmallInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column(
            "minute",
            sa.SmallInteger(),
            nullable=False,
            comment="minute of first purchase; -1 = before horn",
        ),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 5", name=op.f("ck_item_timing_aggregates_position_range")
        ),
        sa.CheckConstraint(
            "rank_bracket BETWEEN 0 AND 8",
            name=op.f("ck_item_timing_aggregates_rank_bracket_range"),
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["patches.patch_id"],
            name=op.f("fk_item_timing_aggregates_patch_id_patches"),
        ),
        sa.PrimaryKeyConstraint(
            "patch_id",
            "rank_bracket",
            "hero_id",
            "position",
            "item_id",
            "minute",
            name=op.f("pk_item_timing_aggregates"),
        ),
    )
    op.create_index(
        "ix_item_timing_aggregates_item_patch",
        "item_timing_aggregates",
        ["item_id", "patch_id"],
        unique=False,
    )
    op.create_table(
        "matches",
        sa.Column("match_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration", sa.Integer(), nullable=False, comment="seconds"),
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("game_mode", sa.SmallInteger(), nullable=False),
        sa.Column("lobby_type", sa.SmallInteger(), nullable=False),
        sa.Column(
            "avg_rank_tier",
            sa.SmallInteger(),
            nullable=True,
            comment="OpenDota rank_tier: tens digit = medal (1-8), ones digit = stars",
        ),
        sa.Column("radiant_win", sa.Boolean(), nullable=False),
        sa.Column("region", sa.SmallInteger(), nullable=True),
        sa.Column("is_parsed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "opendota",
                "stratz",
                "replay",
                name="match_source",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "avg_rank_tier BETWEEN 10 AND 85", name=op.f("ck_matches_avg_rank_tier_range")
        ),
        sa.CheckConstraint("duration >= 0", name=op.f("ck_matches_duration_non_negative")),
        sa.ForeignKeyConstraint(
            ["patch_id"], ["patches.patch_id"], name=op.f("fk_matches_patch_id_patches")
        ),
        sa.PrimaryKeyConstraint("match_id", "start_time", name=op.f("pk_matches")),
        postgresql_partition_by="RANGE (start_time)",
    )
    op.create_index("ix_matches_patch_rank", "matches", ["patch_id", "avg_rank_tier"], unique=False)
    op.create_table(
        "patch_bracket_totals",
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column("rank_bracket", sa.SmallInteger(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rank_bracket BETWEEN 0 AND 8", name=op.f("ck_patch_bracket_totals_rank_bracket_range")
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["patches.patch_id"],
            name=op.f("fk_patch_bracket_totals_patch_id_patches"),
        ),
        sa.PrimaryKeyConstraint("patch_id", "rank_bracket", name=op.f("pk_patch_bracket_totals")),
    )
    op.create_table(
        "match_events",
        sa.Column("match_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "seq",
            sa.Integer(),
            autoincrement=False,
            nullable=False,
            comment="order within the match",
        ),
        sa.Column(
            "event_type",
            sa.Enum(
                "purchase",
                "death",
                "ward_placed",
                "ward_destroyed",
                "buyback",
                "rune",
                "roshan",
                "tormentor",
                "teamfight",
                name="event_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "time", sa.Integer(), nullable=False, comment="game seconds; negative before horn"
        ),
        sa.Column("slot", sa.SmallInteger(), nullable=True, comment="acting player, if any"),
        sa.Column("target_slot", sa.SmallInteger(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True, comment="purchase: item bought"),
        sa.Column(
            "key",
            sa.String(length=64),
            nullable=True,
            comment="type-specific discriminator: rune type, ward type, killer unit",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["match_id", "start_time"],
            ["matches.match_id", "matches.start_time"],
            name=op.f("fk_match_events_match_id_matches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("match_id", "start_time", "seq", name=op.f("pk_match_events")),
        postgresql_partition_by="RANGE (start_time)",
    )
    op.create_index(
        "ix_match_events_type_item_time",
        "match_events",
        ["event_type", "item_id", "time"],
        unique=False,
    )
    op.create_table(
        "match_players",
        sa.Column("match_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slot", sa.SmallInteger(), nullable=False, comment="0-4 radiant, 5-9 dire"),
        sa.Column("patch_id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            nullable=True,
            comment="NULL when the profile is private or anonymous",
        ),
        sa.Column("hero_id", sa.SmallInteger(), nullable=False),
        sa.Column("is_radiant", sa.Boolean(), nullable=False),
        sa.Column(
            "position", sa.SmallInteger(), nullable=True, comment="1 carry .. 5 hard support"
        ),
        sa.Column(
            "lane",
            sa.SmallInteger(),
            nullable=True,
            comment="OpenDota lane: 1 bot, 2 mid, 3 top, 4 radiant jungle, 5 dire jungle",
        ),
        sa.Column(
            "lane_role",
            sa.SmallInteger(),
            nullable=True,
            comment="OpenDota lane_role: 1 safe, 2 mid, 3 off, 4 jungle",
        ),
        sa.Column("kills", sa.SmallInteger(), nullable=False),
        sa.Column("deaths", sa.SmallInteger(), nullable=False),
        sa.Column("assists", sa.SmallInteger(), nullable=False),
        sa.Column("gpm", sa.SmallInteger(), nullable=False),
        sa.Column("xpm", sa.SmallInteger(), nullable=False),
        sa.Column("last_hits", sa.SmallInteger(), nullable=False),
        sa.Column("denies", sa.SmallInteger(), nullable=False),
        sa.Column("net_worth", sa.Integer(), nullable=True),
        sa.Column("hero_damage", sa.Integer(), nullable=True),
        sa.Column("tower_damage", sa.Integer(), nullable=True),
        sa.Column("healing", sa.Integer(), nullable=True),
        sa.Column(
            "items",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            comment="final inventory item ids, slots 0-5; 0 = empty",
        ),
        sa.Column("backpack", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("item_neutral", sa.Integer(), nullable=True),
        sa.Column("win", sa.Boolean(), nullable=False),
        sa.CheckConstraint("lane BETWEEN 1 AND 5", name=op.f("ck_match_players_lane_range")),
        sa.CheckConstraint(
            "lane_role BETWEEN 1 AND 4", name=op.f("ck_match_players_lane_role_range")
        ),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 5", name=op.f("ck_match_players_position_range")
        ),
        sa.CheckConstraint("slot BETWEEN 0 AND 9", name=op.f("ck_match_players_slot_range")),
        sa.ForeignKeyConstraint(
            ["match_id", "start_time"],
            ["matches.match_id", "matches.start_time"],
            name=op.f("fk_match_players_match_id_matches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("match_id", "start_time", "slot", name=op.f("pk_match_players")),
        postgresql_partition_by="RANGE (start_time)",
    )
    op.create_index(
        "ix_match_players_account_start_time",
        "match_players",
        ["account_id", "start_time"],
        unique=False,
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )
    op.create_index(
        "ix_match_players_patch_hero_position",
        "match_players",
        ["patch_id", "hero_id", "position"],
        unique=False,
    )
    op.create_table(
        "player_timeseries",
        sa.Column("match_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slot", sa.SmallInteger(), nullable=False),
        sa.Column(
            "gold_t",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            comment="cumulative gold earned",
        ),
        sa.Column("xp_t", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("lh_t", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("dn_t", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.ForeignKeyConstraint(
            ["match_id", "start_time"],
            ["matches.match_id", "matches.start_time"],
            name=op.f("fk_player_timeseries_match_id_matches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "match_id", "start_time", "slot", name=op.f("pk_player_timeseries")
        ),
        postgresql_partition_by="RANGE (start_time)",
    )
    op.execute(ENSURE_MONTH_PARTITIONS)
    op.execute(PRECREATED_MONTHS)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ensure_month_partitions(timestamptz)")
    op.drop_table("player_timeseries")
    op.drop_index("ix_match_players_patch_hero_position", table_name="match_players")
    op.drop_index(
        "ix_match_players_account_start_time",
        table_name="match_players",
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )
    op.drop_table("match_players")
    op.drop_index("ix_match_events_type_item_time", table_name="match_events")
    op.drop_table("match_events")
    op.drop_table("patch_bracket_totals")
    op.drop_index("ix_matches_patch_rank", table_name="matches")
    op.drop_table("matches")
    op.drop_index("ix_item_timing_aggregates_item_patch", table_name="item_timing_aggregates")
    op.drop_table("item_timing_aggregates")
    op.drop_table("hero_matchup_aggregates")
    op.drop_index("ix_hero_aggregates_hero_patch", table_name="hero_aggregates")
    op.drop_table("hero_aggregates")
    op.drop_table("patches")
