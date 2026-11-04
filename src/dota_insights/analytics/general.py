"""Game-wide statistics: side win rates, durations, modes, regions, records, rank distribution.

Side, duration, mode and region figures come from one OpenDota ``/explorer`` SQL query over
``public_matches`` in a recent window of match ids. Records come from ``/records/{field}``.
The rank distribution combines OpenDota ``/distributions`` (player profiles OpenDota knows)
with STRATZ's per-rank player counts and season leaderboard sizes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from dota_insights.analytics.stats import wilson_interval
from dota_insights.cache import TTL_AGGREGATE, TTL_REFERENCE, cached
from dota_insights.models import Filters, GameMode, RankBracket
from dota_insights.reference import hero_by_id
from dota_insights.sources import get_opendota, get_stratz
from dota_insights.sources.stratz import StratzError

# About 11 hours of public matches at November 2026 volume (~250k Ranked All Pick).
WINDOW_MATCH_IDS = 800_000
DURATION_BIN_SECONDS = 300
# OpenDota's avg_rank_tier stops at 75: Immortal players count into the Divine bracket.
HIGHEST_AVG_TIER = 7
DEFAULT_GAME_MODE = GameMode.RANKED_ALL_PICK

# OpenDota game_mode id for each mode, and whether its lobby is ranked matchmaking
# (lobby_type 7); None means any lobby. OpenDota's game_mode 22 is All Pick.
RANKED_LOBBY = 7
GAME_MODE_IDS: dict[GameMode, tuple[int, bool | None]] = {
    GameMode.RANKED_ALL_PICK: (22, True),
    GameMode.ALL_PICK: (22, False),
    GameMode.TURBO: (23, None),
    GameMode.CAPTAINS_MODE: (2, None),
    GameMode.SINGLE_DRAFT: (4, None),
    GameMode.RANDOM_DRAFT: (3, None),
    GameMode.ALL_RANDOM: (5, None),
    GameMode.ABILITY_DRAFT: (18, None),
}

MEDALS = tuple(RankBracket)


# ---------------------------------------------------------------- general stats


class Share(BaseModel):
    name: str = Field(description="Category name (game mode or region).")
    matches: int = Field(description="Matches in this category.")
    share: float = Field(description="matches / total matches of the breakdown, 0..1.")


class DurationBin(BaseModel):
    from_minute: int = Field(description="Bin start, minutes (inclusive).")
    to_minute: int = Field(description="Bin end, minutes (exclusive).")
    matches: int
    share: float = Field(description="Share of the filtered matches in this bin, 0..1.")


class DurationSummary(BaseModel):
    mean_minutes: float
    p25_minutes: float
    median_minutes: float
    p75_minutes: float
    bins: list[DurationBin] = Field(description="Distribution in 5-minute bins.")


class GeneralStats(BaseModel):
    matches: int = Field(description="Matches matching the filters in the window.")
    radiant_wins: int
    radiant_win_rate: float
    dire_win_rate: float
    duration: DurationSummary
    regions: list[Share] = Field(description="Filtered matches by server region, largest first.")
    game_modes: list[Share] = Field(
        description=(
            "All public matches in the window by game mode, ignoring the game_mode and "
            "rank filters, largest first."
        )
    )
    window_matches: int = Field(description="All public matches in the window, any mode.")
    window_start: datetime = Field(description="Start time of the earliest match (UTC).")
    window_end: datetime = Field(description="Start time of the latest match (UTC).")


class UnsupportedFilter(ValueError):
    """A filter that the source cannot apply; the message says which and why."""


def rank_tier_sql(filters: Filters) -> str | None:
    """Predicate on ``avg_rank_tier`` for the rank filter, or None for all brackets."""
    rank_range = filters.rank_range
    if rank_range is None:
        return None
    low, high = rank_range
    if low.tier > HIGHEST_AVG_TIER:
        raise UnsupportedFilter(
            "OpenDota's match-average rank stops at Divine, so Immortal-only matches cannot "
            "be selected. Use rank_bracket 'divine+' (Divine and Immortal together)."
        )
    high_tier = min(high.tier, HIGHEST_AVG_TIER)
    return f"avg_rank_tier >= {low.tier * 10} and avg_rank_tier < {(high_tier + 1) * 10}"


def build_general_sql(filters: Filters, window: int = WINDOW_MATCH_IDS) -> str:
    """Explorer query: filtered side/duration/cluster rows plus window-wide mode counts.

    Every interpolated value comes from an enum or an int, never from caller text.
    """
    game_mode, ranked = GAME_MODE_IDS[filters.game_mode or DEFAULT_GAME_MODE]
    predicates = [f"game_mode = {game_mode}"]
    if ranked is not None:
        predicates.append(f"lobby_type {'=' if ranked else '<>'} {RANKED_LOBBY}")
    rank = rank_tier_sql(filters)
    if rank:
        predicates.append(rank)
    where = " and ".join(predicates)
    return (
        "with bounds as (select max(match_id) as hi from public_matches), "
        "w as (select p.* from public_matches p, bounds "
        f"where p.match_id > bounds.hi - {int(window)} and p.duration > 0), "
        f"f as (select * from w where {where}) "
        "select 'filtered' as part, radiant_win, "
        f"(duration / {DURATION_BIN_SECONDS})::int as bin, cluster, "
        "null::int as game_mode, null::int as lobby_type, count(*)::int as n, "
        "min(start_time) as first_start, max(start_time) as last_start, "
        "avg(duration)::float as mean_duration, "
        "percentile_cont(0.25) within group (order by duration) as p25, "
        "percentile_cont(0.5) within group (order by duration) as p50, "
        "percentile_cont(0.75) within group (order by duration) as p75 "
        "from f group by grouping sets ((), (radiant_win), (bin), (cluster)) "
        "union all "
        "select 'window', null, null, null, game_mode, lobby_type, count(*)::int, "
        "min(start_time), max(start_time), null, null, null, null "
        "from w group by game_mode, lobby_type"
    )


@cached(ttl=TTL_AGGREGATE)
async def fetch_explorer(sql: str) -> list[dict[str, Any]]:
    data = await get_opendota().explorer(sql)
    if data.get("err"):
        raise RuntimeError(f"OpenDota explorer query failed: {data['err']}")
    return data["rows"]


@cached(ttl=TTL_REFERENCE)
async def fetch_constants(resource: str) -> Any:
    return await get_opendota().constants(resource)


def _mode_label(game_mode: int, lobby_type: int, names: dict[str, Any]) -> str:
    for mode, (mode_id, ranked) in GAME_MODE_IDS.items():
        if mode_id == game_mode and ranked in (None, lobby_type == RANKED_LOBBY):
            return mode.value
    raw = names.get(str(game_mode), {}).get("name") or f"game_mode_{game_mode}"
    return raw.removeprefix("game_mode_")


def _shares(counts: dict[str, int]) -> list[Share]:
    total = sum(counts.values())
    return [
        Share(name=name, matches=n, share=n / total)
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def timestamp(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


UNKNOWN_REGION = "unknown"


def cluster_regions(
    od_clusters: dict[str, int], od_regions: dict[str, str], stratz: dict[str, Any] | None
) -> dict[int, str]:
    """Server cluster id -> region name. STRATZ knows newer clusters; OpenDota fills gaps."""
    mapping = {}
    for cluster, region_id in od_clusters.items():
        if str(region_id) in od_regions:
            words = od_regions[str(region_id)].split()
            mapping[int(cluster)] = " ".join(w if len(w) <= 2 else w.title() for w in words)
    if stratz is not None:
        names = {r["id"]: r["clientName"] or r["name"] for r in stratz["constants"]["regions"]}
        for cluster in stratz["constants"]["clusters"]:
            if cluster["regionId"] in names:
                mapping[cluster["id"]] = names[cluster["regionId"]]
    return mapping


def summarize_general(
    rows: list[dict[str, Any]], regions: dict[int, str], modes: dict
) -> GeneralStats | None:
    """Turn explorer rows into GeneralStats; None when no match passed the filters."""
    filtered = [r for r in rows if r["part"] == "filtered"]
    window = [r for r in rows if r["part"] == "window"]
    total = next(
        (
            r
            for r in filtered
            if r["radiant_win"] is None and r["bin"] is None and r["cluster"] is None
        ),
        None,
    )
    if total is None or not total["n"]:
        return None
    n = total["n"]
    radiant_wins = sum(r["n"] for r in filtered if r["radiant_win"] is True)

    bins = sorted((r for r in filtered if r["bin"] is not None), key=lambda r: r["bin"])
    width = DURATION_BIN_SECONDS // 60
    duration = DurationSummary(
        mean_minutes=round(total["mean_duration"] / 60, 2),
        p25_minutes=round(total["p25"] / 60, 2),
        median_minutes=round(total["p50"] / 60, 2),
        p75_minutes=round(total["p75"] / 60, 2),
        bins=[
            DurationBin(
                from_minute=r["bin"] * width,
                to_minute=(r["bin"] + 1) * width,
                matches=r["n"],
                share=r["n"] / n,
            )
            for r in bins
        ],
    )

    by_region: dict[str, int] = {}
    for r in filtered:
        if r["cluster"] is None:
            continue
        name = regions.get(r["cluster"], UNKNOWN_REGION)
        by_region[name] = by_region.get(name, 0) + r["n"]

    by_mode: dict[str, int] = {}
    for r in window:
        label = _mode_label(r["game_mode"], r["lobby_type"], modes)
        by_mode[label] = by_mode.get(label, 0) + r["n"]

    return GeneralStats(
        matches=n,
        radiant_wins=radiant_wins,
        radiant_win_rate=radiant_wins / n,
        dire_win_rate=(n - radiant_wins) / n,
        duration=duration,
        regions=_shares(by_region),
        game_modes=_shares(by_mode),
        window_matches=sum(r["n"] for r in window),
        window_start=timestamp(min(r["first_start"] for r in window)),
        window_end=timestamp(max(r["last_start"] for r in window)),
    )


STRATZ_REGIONS_QUERY = """
query Regions {
  constants {
    clusters { id regionId }
    regions { id name clientName }
  }
}
"""


@cached(ttl=TTL_REFERENCE)
async def fetch_stratz_regions() -> dict[str, Any]:
    return await get_stratz().query(STRATZ_REGIONS_QUERY)


async def general_stats(filters: Filters) -> tuple[GeneralStats | None, bool]:
    """Side, duration, mode and region figures for ``filters``, and whether STRATZ's
    cluster map was used (it is optional; OpenDota's alone misses newer clusters)."""
    sql = build_general_sql(filters)
    rows, od_clusters, od_regions, modes = await asyncio.gather(
        fetch_explorer(sql),
        fetch_constants("cluster"),
        fetch_constants("region"),
        fetch_constants("game_mode"),
    )
    try:
        stratz = await fetch_stratz_regions()
    except StratzError:
        stratz = None
    regions = cluster_regions(od_clusters, od_regions, stratz)
    return summarize_general(rows, regions, modes), stratz is not None


def side_confidence(stats: GeneralStats) -> dict[str, tuple[float, float]]:
    low, high = wilson_interval(stats.radiant_wins, stats.matches)
    return {"radiant_win_rate": (low, high), "dire_win_rate": (1 - high, 1 - low)}


# ---------------------------------------------------------------- records


class RecordMetric(StrEnum):
    """Single-match stats OpenDota keeps records for (``/records/{field}``)."""

    KILLS = "kills"
    DEATHS = "deaths"
    ASSISTS = "assists"
    GOLD_PER_MIN = "gold_per_min"
    XP_PER_MIN = "xp_per_min"
    LAST_HITS = "last_hits"
    DENIES = "denies"
    HERO_DAMAGE = "hero_damage"
    TOWER_DAMAGE = "tower_damage"
    HERO_HEALING = "hero_healing"
    DURATION = "duration"


METRIC_ALIASES: dict[str, RecordMetric] = {
    "gpm": RecordMetric.GOLD_PER_MIN,
    "gold": RecordMetric.GOLD_PER_MIN,
    "xpm": RecordMetric.XP_PER_MIN,
    "xp": RecordMetric.XP_PER_MIN,
    "lh": RecordMetric.LAST_HITS,
    "cs": RecordMetric.LAST_HITS,
    "damage": RecordMetric.HERO_DAMAGE,
    "healing": RecordMetric.HERO_HEALING,
    "heal": RecordMetric.HERO_HEALING,
    "building_damage": RecordMetric.TOWER_DAMAGE,
    "longest": RecordMetric.DURATION,
    "longest_match": RecordMetric.DURATION,
    "length": RecordMetric.DURATION,
    "time": RecordMetric.DURATION,
}


def parse_metric(text: str) -> RecordMetric | None:
    """Map "GPM", "longest match", "hero damage" to a metric; None if unknown."""
    key = text.strip().lower().replace(" ", "_").replace("-", "_")
    try:
        return RecordMetric(key)
    except ValueError:
        return METRIC_ALIASES.get(key)


class RecordRow(BaseModel):
    rank: int = Field(description="Position in OpenDota's top 100 for the metric (1 = best).")
    match_id: int
    start_time: datetime = Field(description="Match start (UTC).")
    hero_id: int | None = Field(description="Hero who set it; null for match-level metrics.")
    hero: str | None
    value: float = Field(description="The record value (seconds for duration).")


class MetricRecords(BaseModel):
    metric: RecordMetric
    unit: str
    rows: list[RecordRow]


METRIC_UNITS = {RecordMetric.DURATION: "seconds"} | {
    m: "per minute" for m in (RecordMetric.GOLD_PER_MIN, RecordMetric.XP_PER_MIN)
}


@cached(ttl=TTL_AGGREGATE)
async def fetch_records(metric: RecordMetric) -> list[dict[str, Any]]:
    return await get_opendota()._get(f"/records/{metric.value}")


def to_records(
    metric: RecordMetric, raw: list[dict[str, Any]], hero_id: int | None, limit: int
) -> MetricRecords:
    heroes = hero_by_id()
    rows = []
    for rank, row in enumerate(raw, start=1):
        if hero_id is not None and row.get("hero_id") != hero_id:
            continue
        hid = row.get("hero_id")
        rows.append(
            RecordRow(
                rank=rank,
                match_id=row["match_id"],
                start_time=timestamp(row["start_time"]),
                hero_id=hid,
                hero=heroes[hid].display_name if hid in heroes else None,
                value=row["score"],
            )
        )
        if len(rows) >= limit:
            break
    return MetricRecords(metric=metric, unit=METRIC_UNITS.get(metric, "count"), rows=rows)


async def records(
    metrics: list[RecordMetric], hero_id: int | None, limit: int
) -> tuple[list[MetricRecords], list[dict[str, Any]]]:
    """Records per metric (filtered to a hero if given) and the raw rows they came from."""
    raws = await asyncio.gather(*(fetch_records(m) for m in metrics))
    result = [to_records(m, raw, hero_id, limit) for m, raw in zip(metrics, raws, strict=True)]
    return result, [row for raw in raws for row in raw]


# ---------------------------------------------------------------- rank distribution


class RankTierRow(BaseModel):
    rank_tier: int = Field(description="Medal * 10 + stars (e.g. 54 = Legend 4); 80 = Immortal.")
    medal: RankBracket
    stars: int | None = Field(description="1-5, or null for Immortal.")
    opendota_players: int
    opendota_share: float
    stratz_players: int | None
    stratz_share: float | None


class MedalRow(BaseModel):
    medal: RankBracket
    opendota_players: int
    opendota_share: float = Field(description="Share of ranked OpenDota profiles, 0..1.")
    opendota_cumulative: float = Field(
        description="Share of ranked profiles at or below this medal (percentile at its top)."
    )
    stratz_players: int | None
    stratz_share: float | None = Field(description="Share of ranked STRATZ accounts, 0..1.")


class Leaderboard(BaseModel):
    division: str
    players: int


class RankDistribution(BaseModel):
    opendota_total: int = Field(description="Ranked player profiles in OpenDota's data.")
    stratz_total: int | None = Field(description="Ranked accounts in STRATZ's data.")
    stratz_uncalibrated: int | None = Field(
        description="STRATZ accounts with no rank (rank 0), excluded from the shares."
    )
    medals: list[MedalRow]
    tiers: list[RankTierRow]
    leaderboards: list[Leaderboard] = Field(
        description="Current-season Immortal leaderboard size per division (STRATZ)."
    )


STRATZ_RANK_QUERY = """
query RankDistribution {
  stratz { page { players { steamAccountByRank { rank playerCount } } } }
  leaderboard {
    americas: season(request: {leaderBoardDivision: AMERICAS}) { playerCount }
    europe: season(request: {leaderBoardDivision: EUROPE}) { playerCount }
    seAsia: season(request: {leaderBoardDivision: SE_ASIA}) { playerCount }
    china: season(request: {leaderBoardDivision: CHINA}) { playerCount }
  }
}
"""
DIVISIONS = {"americas": "Americas", "europe": "Europe", "seAsia": "SE Asia", "china": "China"}


@cached(ttl=TTL_AGGREGATE)
async def fetch_distributions() -> tuple[dict[str, Any], datetime]:
    """OpenDota ``/distributions`` and when it was fetched (the payload carries no date)."""
    return await get_opendota()._get("/distributions"), datetime.now(UTC)


@cached(ttl=TTL_AGGREGATE)
async def fetch_stratz_ranks() -> dict[str, Any]:
    return await get_stratz().query(STRATZ_RANK_QUERY)


def _medal(rank_tier: int) -> RankBracket | None:
    tier = rank_tier // 10
    return MEDALS[tier - 1] if 1 <= tier <= len(MEDALS) else None


def rank_distribution(opendota: dict[str, Any], stratz: dict[str, Any] | None) -> RankDistribution:
    od_counts = {
        row["bin"]: row["count"] for row in opendota["ranks"]["rows"] if _medal(row["bin"])
    }
    st_counts: dict[int, int] = {}
    uncalibrated = None
    leaderboards: list[Leaderboard] = []
    if stratz is not None:
        for row in stratz["stratz"]["page"]["players"]["steamAccountByRank"]:
            if _medal(row["rank"]):
                st_counts[row["rank"]] = row["playerCount"]
            elif row["rank"] == 0:
                uncalibrated = row["playerCount"]
        for key, label in DIVISIONS.items():
            board = (stratz.get("leaderboard") or {}).get(key)
            if board and board.get("playerCount") is not None:
                leaderboards.append(Leaderboard(division=label, players=board["playerCount"]))

    od_total = sum(od_counts.values())
    st_total = sum(st_counts.values()) or None
    tiers = [
        RankTierRow(
            rank_tier=tier,
            medal=_medal(tier),
            stars=tier % 10 or None,
            opendota_players=od_counts.get(tier, 0),
            opendota_share=od_counts.get(tier, 0) / od_total,
            stratz_players=st_counts.get(tier) if st_total else None,
            stratz_share=st_counts.get(tier, 0) / st_total if st_total else None,
        )
        for tier in sorted(set(od_counts) | set(st_counts))
    ]
    medals = []
    cumulative = 0
    for medal in MEDALS:
        od = sum(t.opendota_players for t in tiers if t.medal is medal)
        st = sum(t.stratz_players or 0 for t in tiers if t.medal is medal)
        cumulative += od
        medals.append(
            MedalRow(
                medal=medal,
                opendota_players=od,
                opendota_share=od / od_total,
                opendota_cumulative=cumulative / od_total,
                stratz_players=st if st_total else None,
                stratz_share=st / st_total if st_total else None,
            )
        )
    return RankDistribution(
        opendota_total=od_total,
        stratz_total=st_total,
        stratz_uncalibrated=uncalibrated,
        medals=medals,
        tiers=tiers,
        leaderboards=leaderboards,
    )
