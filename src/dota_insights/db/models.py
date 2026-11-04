"""SQLAlchemy models for raw match data and per-patch aggregates.

Per-match tables (``matches``, ``match_players``, ``player_timeseries``, ``match_events``) are
range-partitioned by ``start_time`` into monthly partitions, so every one of them carries
``start_time`` and includes it in its primary key. See docs/architecture/db-schema.md.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

PARTITION_BY_START_TIME = "RANGE (start_time)"

# Parents that get one partition per calendar month; see ensure_month_partitions() in the migration.
PARTITIONED_TABLES = ("matches", "match_players", "player_timeseries", "match_events")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class MatchSource(StrEnum):
    OPENDOTA = "opendota"
    STRATZ = "stratz"
    REPLAY = "replay"


class EventType(StrEnum):
    PURCHASE = "purchase"
    DEATH = "death"
    WARD_PLACED = "ward_placed"
    WARD_DESTROYED = "ward_destroyed"
    BUYBACK = "buyback"
    RUNE = "rune"
    ROSHAN = "roshan"
    TORMENTOR = "tormentor"
    TEAMFIGHT = "teamfight"


class MatchupRelation(StrEnum):
    WITH = "with"  # same team
    VS = "vs"  # opposing team


def _str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """A VARCHAR + CHECK enum: adding a value later is a constraint change, not ALTER TYPE."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Patch(Base):
    """A game patch (e.g. 7.39d). ``released_at`` bounds which matches belong to it."""

    __tablename__ = "patches"

    patch_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(16), unique=True)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint("duration >= 0", name="duration_non_negative"),
        CheckConstraint("avg_rank_tier BETWEEN 10 AND 85", name="avg_rank_tier_range"),
        Index("ix_matches_patch_rank", "patch_id", "avg_rank_tier"),
        {"postgresql_partition_by": PARTITION_BY_START_TIME},
    )

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    duration: Mapped[int] = mapped_column(Integer, comment="seconds")
    patch_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("patches.patch_id"))
    game_mode: Mapped[int] = mapped_column(SmallInteger)
    lobby_type: Mapped[int] = mapped_column(SmallInteger)
    avg_rank_tier: Mapped[int | None] = mapped_column(
        SmallInteger, comment="OpenDota rank_tier: tens digit = medal (1-8), ones digit = stars"
    )
    radiant_win: Mapped[bool] = mapped_column(Boolean)
    region: Mapped[int | None] = mapped_column(SmallInteger)
    is_parsed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    source: Mapped[MatchSource] = mapped_column(_str_enum(MatchSource, "match_source"))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MatchPlayer(Base):
    """One row per player per match; ``patch_id`` is copied from ``matches`` to skip a join."""

    __tablename__ = "match_players"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id", "start_time"],
            ["matches.match_id", "matches.start_time"],
            ondelete="CASCADE",
        ),
        CheckConstraint("slot BETWEEN 0 AND 9", name="slot_range"),
        CheckConstraint("position BETWEEN 1 AND 5", name="position_range"),
        CheckConstraint("lane BETWEEN 1 AND 5", name="lane_range"),
        CheckConstraint("lane_role BETWEEN 1 AND 4", name="lane_role_range"),
        Index("ix_match_players_patch_hero_position", "patch_id", "hero_id", "position"),
        Index(
            "ix_match_players_account_start_time",
            "account_id",
            "start_time",
            postgresql_where=text("account_id IS NOT NULL"),
        ),
        {"postgresql_partition_by": PARTITION_BY_START_TIME},
    )

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    slot: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, comment="0-4 radiant, 5-9 dire"
    )
    patch_id: Mapped[int] = mapped_column(SmallInteger)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="NULL when the profile is private or anonymous"
    )
    hero_id: Mapped[int] = mapped_column(SmallInteger)
    is_radiant: Mapped[bool] = mapped_column(Boolean)
    position: Mapped[int | None] = mapped_column(SmallInteger, comment="1 carry .. 5 hard support")
    lane: Mapped[int | None] = mapped_column(
        SmallInteger, comment="OpenDota lane: 1 bot, 2 mid, 3 top, 4 radiant jungle, 5 dire jungle"
    )
    lane_role: Mapped[int | None] = mapped_column(
        SmallInteger, comment="OpenDota lane_role: 1 safe, 2 mid, 3 off, 4 jungle"
    )
    kills: Mapped[int] = mapped_column(SmallInteger)
    deaths: Mapped[int] = mapped_column(SmallInteger)
    assists: Mapped[int] = mapped_column(SmallInteger)
    gpm: Mapped[int] = mapped_column(SmallInteger)
    xpm: Mapped[int] = mapped_column(SmallInteger)
    last_hits: Mapped[int] = mapped_column(SmallInteger)
    denies: Mapped[int] = mapped_column(SmallInteger)
    net_worth: Mapped[int | None] = mapped_column(Integer)
    hero_damage: Mapped[int | None] = mapped_column(Integer)
    tower_damage: Mapped[int | None] = mapped_column(Integer)
    healing: Mapped[int | None] = mapped_column(Integer)
    items: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), comment="final inventory item ids, slots 0-5; 0 = empty"
    )
    backpack: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    item_neutral: Mapped[int | None] = mapped_column(Integer)
    win: Mapped[bool] = mapped_column(Boolean)


class PlayerTimeseries(Base):
    """Per-minute curves for one player, as arrays indexed by minute (element 1 = minute 0).

    One row per player per match instead of one row per minute: sources deliver arrays, tools
    read whole curves or a single minute (``gold_t[11]`` = gold at 10:00), and rows-per-minute
    would multiply row count ~40x with a 24-byte tuple header per value.
    """

    __tablename__ = "player_timeseries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id", "start_time"],
            ["matches.match_id", "matches.start_time"],
            ondelete="CASCADE",
        ),
        {"postgresql_partition_by": PARTITION_BY_START_TIME},
    )

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    slot: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    gold_t: Mapped[list[int]] = mapped_column(ARRAY(Integer), comment="cumulative gold earned")
    xp_t: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    lh_t: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    dn_t: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))


class MatchEvent(Base):
    """Timed events from parsed matches, all types in one table.

    Every event has a match, a game time and usually an actor slot; the columns queried by
    tools (``event_type``, ``slot``, ``item_id``, ``key``) are typed, and the type-specific
    rest (gold lost on death, teamfight participants, ward coordinates) goes to ``payload``.
    """

    __tablename__ = "match_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id", "start_time"],
            ["matches.match_id", "matches.start_time"],
            ondelete="CASCADE",
        ),
        Index("ix_match_events_type_item_time", "event_type", "item_id", "time"),
        {"postgresql_partition_by": PARTITION_BY_START_TIME},
    )

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    seq: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, comment="order within the match"
    )
    event_type: Mapped[EventType] = mapped_column(_str_enum(EventType, "event_type"))
    time: Mapped[int] = mapped_column(Integer, comment="game seconds; negative before horn")
    slot: Mapped[int | None] = mapped_column(SmallInteger, comment="acting player, if any")
    target_slot: Mapped[int | None] = mapped_column(SmallInteger)
    item_id: Mapped[int | None] = mapped_column(Integer, comment="purchase: item bought")
    key: Mapped[str | None] = mapped_column(
        String(64), comment="type-specific discriminator: rune type, ward type, killer unit"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class _AggregateMixin:
    """Additive counters: refresh jobs upsert sums so averages and win rates stay mergeable."""

    games: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


RANK_BRACKET_CHECK = "rank_bracket BETWEEN 0 AND 8"  # 0 = unknown, 1 Herald .. 8 Immortal


class PatchBracketTotal(Base, _AggregateMixin):
    """Match count per patch and rank bracket: the denominator for pick rate."""

    __tablename__ = "patch_bracket_totals"
    __table_args__ = (CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),)

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)


class HeroAggregate(Base, _AggregateMixin):
    __tablename__ = "hero_aggregates"
    __table_args__ = (
        CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),
        CheckConstraint("position BETWEEN 0 AND 5", name="position_range"),
        Index("ix_hero_aggregates_hero_patch", "hero_id", "patch_id"),
    )

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    position: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, comment="1-5; 0 = position unknown"
    )
    hero_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    sum_kills: Mapped[int] = mapped_column(BigInteger)
    sum_deaths: Mapped[int] = mapped_column(BigInteger)
    sum_assists: Mapped[int] = mapped_column(BigInteger)
    sum_gpm: Mapped[int] = mapped_column(BigInteger)
    sum_xpm: Mapped[int] = mapped_column(BigInteger)
    sum_last_hits: Mapped[int] = mapped_column(BigInteger)
    sum_net_worth: Mapped[int] = mapped_column(BigInteger)
    sum_hero_damage: Mapped[int] = mapped_column(BigInteger)
    sum_tower_damage: Mapped[int] = mapped_column(BigInteger)
    sum_healing: Mapped[int] = mapped_column(BigInteger)
    sum_duration: Mapped[int] = mapped_column(BigInteger, comment="seconds")


class HeroMatchupAggregate(Base, _AggregateMixin):
    """Games and wins of ``hero_id`` with or against ``other_hero_id``."""

    __tablename__ = "hero_matchup_aggregates"
    __table_args__ = (
        CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),
        CheckConstraint("hero_id <> other_hero_id", name="distinct_heroes"),
    )

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    hero_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    relation: Mapped[MatchupRelation] = mapped_column(
        _str_enum(MatchupRelation, "matchup_relation"), primary_key=True
    )
    other_hero_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)


class ItemTimingAggregate(Base, _AggregateMixin):
    """Histogram of first-purchase minute per hero, position and item, from parsed matches."""

    __tablename__ = "item_timing_aggregates"
    __table_args__ = (
        CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),
        CheckConstraint("position BETWEEN 0 AND 5", name="position_range"),
        Index("ix_item_timing_aggregates_item_patch", "item_id", "patch_id"),
    )

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    hero_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    minute: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, comment="minute of first purchase; -1 = before horn"
    )
