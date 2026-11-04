"""SQLAlchemy models for raw match data and per-patch aggregates.

Per-match tables (``matches``, ``match_players``, ``player_timeseries``, ``match_events``) are
range-partitioned by ``start_time`` into monthly partitions, so every one of them carries
``start_time`` and includes it in its primary key. See docs/architecture/db-schema.md.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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

# PostgreSQL types with a SQLite fallback, so collector logic can be tested without PostgreSQL.
IntArray = ARRAY(Integer).with_variant(JSON(), "sqlite")
JsonB = JSONB().with_variant(JSON(), "sqlite")
AutoId = BigInteger().with_variant(Integer(), "sqlite")

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


class HistogramStat(StrEnum):
    GPM = "gpm"
    XPM = "xpm"
    NET_WORTH = "net_worth"


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
        Index(
            "ix_matches_pending_aggregation",
            "patch_id",
            postgresql_where=text("aggregated_at IS NULL"),
        ),
        Index(
            "ix_matches_pending_details_aggregation",
            "patch_id",
            postgresql_where=text("has_details AND details_aggregated_at IS NULL"),
        ),
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
    has_details: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("false"),
        comment="per-player stats and positions are present, not just the draft",
    )
    detail_attempts: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0"), comment="enrichment lookups that found nothing"
    )
    banned_hero_ids: Mapped[list[int] | None] = mapped_column(
        IntArray, comment="NULL = bans unknown (public match bans are hidden by the sources)"
    )
    aggregated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="draft-level aggregates include this match"
    )
    details_aggregated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="detail-level aggregates include this match"
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
    # Player stats are NULL on draft-only rows (from publicMatches), filled in by enrichment.
    kills: Mapped[int | None] = mapped_column(SmallInteger)
    deaths: Mapped[int | None] = mapped_column(SmallInteger)
    assists: Mapped[int | None] = mapped_column(SmallInteger)
    gpm: Mapped[int | None] = mapped_column(SmallInteger)
    xpm: Mapped[int | None] = mapped_column(SmallInteger)
    last_hits: Mapped[int | None] = mapped_column(SmallInteger)
    denies: Mapped[int | None] = mapped_column(SmallInteger)
    net_worth: Mapped[int | None] = mapped_column(Integer)
    hero_damage: Mapped[int | None] = mapped_column(Integer)
    tower_damage: Mapped[int | None] = mapped_column(Integer)
    healing: Mapped[int | None] = mapped_column(Integer)
    items: Mapped[list[int] | None] = mapped_column(
        IntArray, comment="final inventory item ids, slots 0-5; 0 = empty"
    )
    backpack: Mapped[list[int] | None] = mapped_column(IntArray)
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
    gold_t: Mapped[list[int]] = mapped_column(IntArray, comment="cumulative gold earned")
    xp_t: Mapped[list[int]] = mapped_column(IntArray)
    lh_t: Mapped[list[int]] = mapped_column(IntArray)
    dn_t: Mapped[list[int] | None] = mapped_column(IntArray)


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
    payload: Mapped[dict[str, Any]] = mapped_column(JsonB, server_default=text("'{}'"))


class _AggregateMixin:
    """Additive counters: refresh jobs upsert sums so averages and win rates stay mergeable."""

    games: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


RANK_BRACKET_CHECK = "rank_bracket BETWEEN 0 AND 8"  # 0 = unknown, 1 Herald .. 8 Immortal


class PatchBracketTotal(Base, _AggregateMixin):
    """Match count per patch and rank bracket: the denominator for pick rate.

    ``games`` counts every match (draft level); ``detailed_games`` only matches with player
    stats (the denominator for ``hero_aggregates``); ``ban_sample_games`` only matches whose bans
    are known (the denominator for ban rate).
    """

    __tablename__ = "patch_bracket_totals"
    __table_args__ = (CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),)

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    detailed_games: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    ban_sample_games: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class HeroDraftAggregate(Base, _AggregateMixin):
    """Games, wins and bans per hero from every collected match, positions unknown.

    The large-sample source for win and pick rate; ``hero_aggregates`` has the smaller,
    position-resolved sample from enriched matches.
    """

    __tablename__ = "hero_draft_aggregates"
    __table_args__ = (CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),)

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    hero_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    bans: Mapped[int] = mapped_column(Integer, server_default=text("0"))


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


class HeroStatHistogram(Base, _AggregateMixin):
    """Histogram of a per-player stat (GPM, XPM, net worth) for percentiles.

    ``bucket`` is the lower bound of a fixed-width bin (``HISTOGRAM_BUCKET_WIDTH``); percentiles
    are interpolated from the cumulative counts and stay mergeable across brackets and positions.
    """

    __tablename__ = "hero_stat_histograms"
    __table_args__ = (
        CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),
        CheckConstraint("position BETWEEN 0 AND 5", name="position_range"),
    )

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    hero_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    stat: Mapped[HistogramStat] = mapped_column(
        _str_enum(HistogramStat, "histogram_stat"), primary_key=True
    )
    bucket: Mapped[int] = mapped_column(Integer, primary_key=True)


HISTOGRAM_BUCKET_WIDTH = {
    HistogramStat.GPM: 25,
    HistogramStat.XPM: 25,
    HistogramStat.NET_WORTH: 1000,
}


class AggregateStatus(Base):
    """Freshness of one patch's aggregates. Stale patches are rebuilt from the raw tables."""

    __tablename__ = "aggregate_status"

    patch_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("patches.patch_id"), primary_key=True
    )
    stale: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    stale_reason: Mapped[str | None] = mapped_column(String(200))
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_match_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="start time of the oldest match in the draft aggregates"
    )
    last_match_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="start time of the newest match in the draft aggregates"
    )


class HeroStatsDaily(Base):
    """Daily snapshot of OpenDota ``/heroStats`` (its own trailing-window pick/win counts).

    ``scope`` is ``pub`` (``rank_bracket`` 1-8), ``pro`` or ``turbo`` (``rank_bracket`` 0).
    ``bans`` is only known for ``pro``.
    """

    __tablename__ = "hero_stats_daily"
    __table_args__ = (CheckConstraint(RANK_BRACKET_CHECK, name="rank_bracket_range"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    rank_bracket: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    hero_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    picks: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer)
    bans: Mapped[int | None] = mapped_column(Integer)


class ReferenceSnapshot(Base):
    """A distinct version of an OpenDota ``/constants/{resource}`` payload."""

    __tablename__ = "reference_snapshots"

    resource: Mapped[str] = mapped_column(String(32), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[Any] = mapped_column(JsonB)


class CollectorJobRun(Base):
    """One run of a collector job; the scheduler reads the last run to decide what is due."""

    __tablename__ = "collector_job_runs"
    __table_args__ = (Index("ix_collector_job_runs_job_started_at", "job", "started_at"),)

    run_id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), comment="running | ok | skipped | failed")
    api_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    detail: Mapped[str | None] = mapped_column(String(500))


class CollectorApiUsage(Base):
    """API calls the collector made per source and window (``day`` / ``hour``, UTC)."""

    __tablename__ = "collector_api_usage"

    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    window: Mapped[str] = mapped_column(String(8), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer)


class CollectorCursor(Base):
    """How far each ingest stream (e.g. ``public_matches:5``) has read, by match id."""

    __tablename__ = "collector_cursors"

    stream: Mapped[str] = mapped_column(String(32), primary_key=True)
    newest_match_id: Mapped[int] = mapped_column(
        BigInteger, comment="every settled match up to this id has been read"
    )
    newest_start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), comment="start time at that id: how far behind the stream is"
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CollectorGap(Base):
    """A match id range (exclusive bounds) a stream skipped and the backfill job still owes."""

    __tablename__ = "collector_gaps"
    __table_args__ = (
        CheckConstraint("low_match_id < high_match_id", name="ordered_bounds"),
        Index(
            "ix_collector_gaps_stream_open", "stream", postgresql_where=text("filled_at IS NULL")
        ),
    )

    gap_id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    stream: Mapped[str] = mapped_column(String(32))
    low_match_id: Mapped[int] = mapped_column(BigInteger)
    high_match_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageEvent(Base):
    """One MCP tool call (p4-08), written from the server through the Redis usage queue.

    Never holds a raw IP: ``client_hash`` is a salted hash whose salt rotates weekly and lives
    only in Redis. ``arguments`` keeps the call's shape: player identifiers are replaced and
    strings are cut to 200 characters (see ``dota_insights.usage.events``).
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_ts", "ts"),
        Index("ix_usage_events_tool_ts", "tool", "ts"),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[Any] = mapped_column(JsonB)
    status: Mapped[str] = mapped_column(String(24), comment="ok | insufficient_data | error")
    error_kind: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(String(300), comment="why the call was not ok")
    latency_ms: Mapped[int] = mapped_column(Integer)
    sources: Mapped[Any] = mapped_column(JsonB, comment="sources called, e.g. [opendota]")
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    client_name: Mapped[str | None] = mapped_column(String(100))
    client_version: Mapped[str | None] = mapped_column(String(50))
    client_hash: Mapped[str | None] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(
        String(8), server_default=text("'mcp'"), comment="mcp | api (the /api/v1 REST routes)"
    )


class UsageFeedback(Base):
    """A wrong-answer report (p5-02) or an unmet question / feature request (p5-03)."""

    __tablename__ = "usage_feedback"
    __table_args__ = (Index("ix_usage_feedback_ts", "ts"),)

    feedback_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16), comment="wrong_answer | feature_request")
    tool: Mapped[str | None] = mapped_column(String(64))
    call_id: Mapped[str | None] = mapped_column(
        String(32), comment="the client's latest call of that tool, when it could be found"
    )
    text: Mapped[str] = mapped_column(String(2000))
    expected: Mapped[str | None] = mapped_column(String(1000))
    client_name: Mapped[str | None] = mapped_column(String(100))
    client_hash: Mapped[str | None] = mapped_column(String(16))


class PlayerOptOut(Base):
    """Players who asked not to be shown (p4-10): player tools refuse, match tools mask them."""

    __tablename__ = "player_opt_outs"

    account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(200))


class ApiKey(Base):
    """A Developer API key (p5-06). Only an HMAC-SHA256 of the key is stored, never the key."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(12), comment="first characters, to recognise a key")
    name: Mapped[str] = mapped_column(String(100), comment="who or what the key is for")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    per_minute: Mapped[int | None] = mapped_column(
        Integer, comment="calls a minute; null: settings.api_key_per_minute"
    )
    per_day: Mapped[int | None] = mapped_column(
        Integer, comment="calls a day; null: settings.api_key_per_day"
    )
