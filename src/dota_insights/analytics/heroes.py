"""Hero meta analytics: overview, side-by-side comparison, tier list and hidden gems.

Win and pick counts come from STRATZ ``heroStats.winDay`` / ``winWeek`` (single medals,
Immortal included, by position and game mode). Per-game averages (KDA, XP, net worth) come
from STRATZ ``heroStats.stats``, which only knows paired brackets. When STRATZ cannot be
reached, win and pick counts fall back to OpenDota ``/heroStats`` (per bracket 1-7, no
positions, Immortal merged into Divine).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field

from dota_insights.analytics.stats import (
    benjamini_hochberg,
    smoothed_rate,
    two_proportion_ztest,
    wilson_interval,
)
from dota_insights.cache import TTL_AGGREGATE, TTL_REFERENCE, cached
from dota_insights.mcp.envelope import Period, Source
from dota_insights.models.filters import Filters, GameMode, Position, RankBracket
from dota_insights.reference import Hero, hero_by_id
from dota_insights.sources import get_opendota, get_stratz
from dota_insights.sources.stratz import StratzError

DEFAULT_GAME_MODE = GameMode.RANKED_ALL_PICK
# Most recent daily buckets read for the current patch, and weekly buckets for older ones.
DAYS_TAKE = 14
WEEKS_TAKE = 60
TREND_WEEKS = 40
TREND_PATCHES = 3
# Empirical-Bayes prior strength: pseudo-games at the pool's mean win rate.
PRIOR_STRENGTH = 500
# Tier cut-offs on (smoothed win rate - pool win rate), in win-rate points.
TIER_CUTOFFS: tuple[tuple[str, float], ...] = (
    ("S", 0.025),
    ("A", 0.01),
    ("B", -0.01),
    ("C", -0.025),
)
GEM_MIN_EDGE = 0.01
SIGNIFICANCE = 0.05

STRATZ_GAME_MODES = {
    GameMode.RANKED_ALL_PICK: "ALL_PICK_RANKED",
    GameMode.ALL_PICK: "ALL_PICK",
    GameMode.TURBO: "TURBO",
    GameMode.CAPTAINS_MODE: "CAPTAINS_MODE",
    GameMode.SINGLE_DRAFT: "SINGLE_DRAFT",
    GameMode.RANDOM_DRAFT: "RANDOM_DRAFT",
    GameMode.ALL_RANDOM: "ALL_RANDOM",
    GameMode.ABILITY_DRAFT: "ABILITY_DRAFT",
}
BASIC_BRACKETS = (
    ("HERALD_GUARDIAN", 1, 2),
    ("CRUSADER_ARCHON", 3, 4),
    ("LEGEND_ANCIENT", 5, 6),
    ("DIVINE_IMMORTAL", 7, 8),
)

_WIN_ARGS = (
    "$heroIds: [Short], $bracketIds: [RankBracket], $positionIds: [MatchPlayerPositionType], "
    "$gameModeIds: [GameModeEnumType], $take: Int"
)
_WIN_FILTER = (
    "heroIds: $heroIds, positionIds: $positionIds, gameModeIds: $gameModeIds, take: $take, "
    "groupBy: HERO_ID"
)
WIN_DAY_QUERY = f"""query HeroWinDay({_WIN_ARGS}) {{
  heroStats {{
    winDay({_WIN_FILTER}, bracketIds: $bracketIds) {{ day heroId winCount matchCount }}
  }}
}}"""
WIN_WEEK_QUERY = f"""query HeroWinWeek({_WIN_ARGS}) {{
  heroStats {{
    winWeek({_WIN_FILTER}, bracketIds: $bracketIds) {{ week heroId winCount matchCount }}
  }}
}}"""


def _breakdown_query() -> str:
    """One winDay per medal (fixed position filter) and per position (fixed bracket filter)."""
    fields = "day heroId winCount matchCount"
    common = "heroIds: $heroIds, gameModeIds: $gameModeIds, take: $take, groupBy: HERO_ID"
    parts = [
        f"b_{b.value}: winDay({common}, positionIds: $positionIds, "
        f"bracketIds: [{b.name}]) {{ {fields} }}"
        for b in RankBracket
    ] + [
        f"p_{p.value}: winDay({common}, bracketIds: $bracketIds, "
        f"positionIds: [POSITION_{p.value}]) {{ {fields} }}"
        for p in Position
    ]
    body = "\n    ".join(parts)
    return f"query HeroBreakdown({_WIN_ARGS}) {{\n  heroStats {{\n    {body}\n  }}\n}}"


BREAKDOWN_QUERY = _breakdown_query()
HERO_STATS_QUERY = """query HeroAverages($heroIds: [Short],
    $bracketBasicIds: [RankBracketBasicEnum], $positionIds: [MatchPlayerPositionType]) {
  heroStats {
    stats(heroIds: $heroIds, bracketBasicIds: $bracketBasicIds, positionIds: $positionIds) {
      heroId matchCount winCount kills deaths assists networth xp cs time
    }
  }
}"""
GAME_VERSIONS_QUERY = "query GameVersions { constants { gameVersions { id name asOfDateTime } } }"


class HeroDataUnavailable(Exception):
    """Neither source can answer these filters. ``hint`` says what to change."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# Patches and filters


@dataclass(frozen=True, slots=True)
class PatchWindow:
    name: str
    start: datetime
    end: datetime | None  # None while the patch is live
    current: bool


@dataclass(frozen=True, slots=True)
class GameVersion:
    name: str
    start: datetime


@cached(ttl=TTL_REFERENCE)
async def game_versions() -> tuple[GameVersion, ...]:
    """STRATZ game versions (lettered patches) in release order."""
    data = await get_stratz().query(GAME_VERSIONS_QUERY)
    versions = [
        GameVersion(v["name"], datetime.fromtimestamp(v["asOfDateTime"], UTC))
        for v in data["constants"]["gameVersions"]
    ]
    return tuple(sorted(versions, key=lambda v: v.start))


def patch_window(versions: tuple[GameVersion, ...], patch: str | None) -> PatchWindow | None:
    """Date range of ``patch`` ("7.41" spans 7.41-7.41f, "7.41d" only itself; None = latest).

    Returns None when the patch is unknown.
    """
    if not versions:
        return None
    if patch is None:
        last = versions[-1]
        return PatchWindow(last.name, last.start, None, True)
    text = patch.strip().lower()
    idx = [
        i
        for i, v in enumerate(versions)
        if v.name == text or (v.name.startswith(text) and v.name[len(text) :].isalpha())
    ]
    if not idx:
        return None
    first, last = idx[0], idx[-1]
    end = versions[last + 1].start if last + 1 < len(versions) else None
    return PatchWindow(text, versions[first].start, end, end is None)


def bracket_ids(filters: Filters) -> list[str] | None:
    """STRATZ ``RankBracket`` names covered by the filter (None = all brackets)."""
    rng = filters.rank_range
    if rng is None:
        return None
    lo, hi = rng
    return [b.name for b in RankBracket if lo.tier <= b.tier <= hi.tier]


def basic_bracket_ids(filters: Filters) -> tuple[list[str] | None, bool]:
    """Paired STRATZ brackets overlapping the filter, and whether they match it exactly."""
    rng = filters.rank_range
    if rng is None:
        return None, True
    lo, hi = rng
    pairs = [(name, a, b) for name, a, b in BASIC_BRACKETS if b >= lo.tier and a <= hi.tier]
    exact = pairs[0][1] == lo.tier and pairs[-1][2] == hi.tier
    return [name for name, _, _ in pairs], exact


def position_ids(filters: Filters) -> list[str] | None:
    return None if filters.position is None else [f"POSITION_{int(filters.position)}"]


def effective_filters(filters: Filters, patch: str | None = None) -> Filters:
    """Filters with the defaults this module applied filled in."""
    update: dict[str, Any] = {}
    if filters.game_mode is None:
        update["game_mode"] = DEFAULT_GAME_MODE
    if patch is not None:
        update["patch"] = patch
    return filters.model_copy(update=update)


# ---------------------------------------------------------------------------
# Hero win/pick pool


@dataclass(frozen=True, slots=True)
class Count:
    matches: int
    wins: int

    def __add__(self, other: Count) -> Count:
        return Count(self.matches + other.matches, self.wins + other.wins)


@dataclass(frozen=True, slots=True)
class Pool:
    """Win and pick counts for every hero over one set of filters."""

    counts: dict[int, Count]
    total_matches: int  # matches in the pool (each counted once)
    filters: Filters
    source: Source
    patch: str | None
    period: Period | None
    freshness: datetime | None

    @property
    def win_rate(self) -> float:
        matches = sum(c.matches for c in self.counts.values())
        wins = sum(c.wins for c in self.counts.values())
        return wins / matches if matches else 0.5

    def pick_rate(self, hero_id: int) -> float:
        count = self.counts.get(hero_id, Count(0, 0))
        return count.matches / self.total_matches if self.total_matches else 0.0


def _variables(filters: Filters, hero_ids: list[int] | None, take: int) -> dict[str, Any]:
    mode = filters.game_mode or DEFAULT_GAME_MODE
    return {
        "heroIds": hero_ids,
        "bracketIds": bracket_ids(filters),
        "positionIds": position_ids(filters),
        "gameModeIds": [STRATZ_GAME_MODES[mode]],
        "take": take,
    }


@cached(ttl=TTL_AGGREGATE)
async def _stratz_query(document: str, variables_key: tuple) -> Any:
    return await get_stratz().query(document, dict(variables_key))


async def _query(document: str, variables: dict[str, Any]) -> Any:
    key = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in variables.items()))
    return await _stratz_query(document, key)


def _bucket_start(row: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(row["day"] if "day" in row else row["week"], UTC)


def _in_window(start: datetime, window: PatchWindow) -> bool:
    return start >= window.start.replace(hour=0, minute=0, second=0) and (
        window.end is None or start < window.end
    )


def _sum_rows(rows: list[dict[str, Any]]) -> dict[int, Count]:
    counts: dict[int, Count] = {}
    for row in rows:
        hero_id = row["heroId"]
        counts[hero_id] = counts.get(hero_id, Count(0, 0)) + Count(
            row["matchCount"], row["winCount"]
        )
    return counts


def _period(
    rows: list[dict[str, Any]], window: PatchWindow
) -> tuple[Period | None, datetime | None]:
    if not rows:
        return None, None
    starts = sorted({_bucket_start(r) for r in rows})
    span = timedelta(days=1) if "day" in rows[0] else timedelta(days=7)
    last_end = starts[-1] + span
    if window.end is not None:
        last_end = min(last_end, window.end)
    to_date = (last_end - timedelta(seconds=1)).date()
    return Period(from_date=starts[0].date(), to_date=max(to_date, starts[0].date())), last_end


async def _window_rows(
    filters: Filters, window: PatchWindow, hero_ids: list[int] | None
) -> list[dict[str, Any]]:
    """Daily rows for the live patch, weekly rows for a finished one, inside ``window``."""
    if window.current:
        data = await _query(WIN_DAY_QUERY, _variables(filters, hero_ids, DAYS_TAKE))
        rows = data["heroStats"]["winDay"]
    else:
        data = await _query(WIN_WEEK_QUERY, _variables(filters, hero_ids, WEEKS_TAKE))
        rows = data["heroStats"]["winWeek"]
    return [r for r in rows if _in_window(_bucket_start(r), window)]


async def stratz_pool(filters: Filters) -> Pool:
    versions = await game_versions()
    window = patch_window(versions, filters.patch)
    if window is None:
        known = ", ".join(v.name for v in versions[-6:])
        raise HeroDataUnavailable(
            f"Unknown patch {filters.patch!r}.", f"Use a known patch such as one of: {known}."
        )
    rows = await _window_rows(filters, window, None)
    counts = _sum_rows(rows)
    players_per_match = 2 if filters.position is not None else 10
    total = round(sum(c.matches for c in counts.values()) / players_per_match)
    period, freshness = _period(rows, window)
    return Pool(
        counts=counts,
        total_matches=total,
        filters=effective_filters(filters, window.name),
        source="stratz",
        patch=window.name,
        period=period,
        freshness=freshness,
    )


@cached(ttl=TTL_AGGREGATE)
async def _opendota_hero_stats() -> list[dict[str, Any]]:
    return await get_opendota().hero_stats()


async def opendota_pool(filters: Filters) -> Pool:
    """Pool from OpenDota ``/heroStats`` (last ~7 days, brackets 1-7, no positions)."""
    if filters.position is not None:
        raise HeroDataUnavailable(
            "Per-position hero stats need STRATZ, which is unavailable right now.",
            "Drop the position filter or retry later.",
        )
    if filters.patch is not None:
        raise HeroDataUnavailable(
            "OpenDota hero aggregates only cover the last week, not a chosen patch.",
            "Drop the patch filter or retry later, when STRATZ is reachable.",
        )
    rng = filters.rank_range
    tiers = range(1, 8) if rng is None else range(rng[0].tier, min(rng[1].tier, 7) + 1)
    if not tiers:
        tiers = range(7, 8)
    counts = {}
    for row in await _opendota_hero_stats():
        counts[row["id"]] = Count(
            sum(row.get(f"{t}_pick") or 0 for t in tiers),
            sum(row.get(f"{t}_win") or 0 for t in tiers),
        )
    total = round(sum(c.matches for c in counts.values()) / 10)
    today = datetime.now(UTC).date()
    return Pool(
        counts=counts,
        total_matches=total,
        filters=filters,
        source="opendota",
        patch=None,
        period=Period(from_date=today - timedelta(days=7), to_date=today),
        freshness=None,
    )


def _stratz_failed(exc: Exception) -> bool:
    return isinstance(exc, StratzError)


async def hero_pool(filters: Filters) -> Pool:
    """STRATZ pool, or OpenDota's when STRATZ fails."""
    try:
        return await stratz_pool(filters)
    except HeroDataUnavailable:
        raise
    except Exception as exc:
        if not _stratz_failed(exc):
            raise
        return await opendota_pool(filters)


def pool_caveats(pool: Pool) -> list[str]:
    if pool.source == "stratz":
        notes = [
            "Data from STRATZ (https://stratz.com).",
            f"Counts are STRATZ daily (live patch, last {DAYS_TAKE} days with data) or weekly "
            "(older patch) buckets; a bucket that straddles a patch day is assigned by its "
            "start date.",
        ]
    else:
        notes = [
            "STRATZ was unavailable, so counts come from OpenDota /heroStats: about the last "
            "7 days of the matches OpenDota samples, not split by position or game mode.",
        ]
        rng = pool.filters.rank_range
        if rng is None or rng[1] is RankBracket.IMMORTAL:
            notes.append("OpenDota merges Immortal into Divine (bracket 7).")
    return notes


# ---------------------------------------------------------------------------
# Result models


Interval = tuple[float, float]


class HeroRate(BaseModel):
    hero: str
    hero_id: int
    matches: int = Field(description="Games the hero played under the filters.")
    wins: int
    win_rate: float
    win_rate_ci: Interval
    pick_rate: float | None = Field(
        default=None, description="Share of matches in the pool that included the hero."
    )
    pick_rate_ci: Interval | None = None


class BreakdownRow(BaseModel):
    group: str = Field(description="Medal or position.")
    matches: int
    win_rate: float | None
    win_rate_ci: Interval | None
    share_of_hero_games: float | None = Field(
        default=None, description="For positions: share of the hero's games at this position."
    )


class PatchRate(BaseModel):
    patch: str
    matches: int
    win_rate: float | None
    win_rate_ci: Interval | None
    partial: bool = Field(description="True when the patch is still live or only partly covered.")


class ProStats(BaseModel):
    pro_matches: int
    picks: int
    bans: int
    pick_rate: float
    ban_rate: float
    win_rate: float | None


class HeroOverview(BaseModel):
    hero: str
    hero_id: int
    roles: list[str]
    primary_attr: str
    attack_type: str
    patch: str | None
    current: HeroRate
    win_rate_rank: int | None = Field(
        description="Rank by raw win rate among heroes with at least min_sample games."
    )
    ranked_heroes: int
    typical_positions: list[str] = Field(
        description="Positions holding at least 20% of the hero's games, most played first."
    )
    by_position: list[BreakdownRow]
    by_bracket: list[BreakdownRow]
    trend: list[PatchRate]
    pro: ProStats | None


class HeroComparison(BaseModel):
    hero: str
    hero_id: int
    matches: int
    values: dict[str, float | None]
    delta_vs_first: dict[str, float | None]
    win_rate_ci: Interval
    pick_rate_ci: Interval | None


class PairTest(BaseModel):
    hero_a: str
    hero_b: str
    win_rate_delta: float = Field(description="Win rate of hero_a minus hero_b.")
    z: float
    p_value: float
    q_value: float = Field(description="Benjamini-Hochberg adjusted p-value over all pairs.")
    significant: bool


class ComparisonResult(BaseModel):
    heroes: list[HeroComparison]
    win_rate_tests: list[PairTest]
    averages_sample: dict[str, int] = Field(
        description="Games behind the per-game averages (STRATZ stats), per hero."
    )


class TierEntry(BaseModel):
    rank: int
    tier: str
    hero: str
    hero_id: int
    matches: int
    win_rate: float
    win_rate_ci: Interval
    smoothed_win_rate: float
    pick_rate: float


class TierList(BaseModel):
    pool_win_rate: float
    heroes: list[TierEntry]
    excluded_low_sample: int = Field(description="Heroes left out for having < min_sample games.")


class HiddenGem(BaseModel):
    hero: str
    hero_id: int
    matches: int
    win_rate: float
    win_rate_ci: Interval
    smoothed_win_rate: float
    edge: float = Field(description="Smoothed win rate minus the pool win rate.")
    pick_rate: float
    p_value: float
    q_value: float
    significant: bool


class HiddenGems(BaseModel):
    pool_win_rate: float
    max_pick_rate: float
    gems: list[HiddenGem]
    candidates_checked: int


# ---------------------------------------------------------------------------
# Calculations


def _name(hero_id: int) -> str:
    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"hero {hero_id}"


def hero_rate(pool: Pool, hero_id: int) -> HeroRate:
    count = pool.counts.get(hero_id, Count(0, 0))
    pick_ci = None
    if pool.total_matches:
        pick_ci = tuple(wilson_interval(min(count.matches, pool.total_matches), pool.total_matches))
    return HeroRate(
        hero=_name(hero_id),
        hero_id=hero_id,
        matches=count.matches,
        wins=count.wins,
        win_rate=count.wins / count.matches if count.matches else 0.0,
        win_rate_ci=tuple(wilson_interval(count.wins, count.matches)),
        pick_rate=pool.pick_rate(hero_id) if pool.total_matches else None,
        pick_rate_ci=pick_ci,
    )


def _breakdown(label: str, rows: list[dict[str, Any]], hero_total: int | None) -> BreakdownRow:
    total = Count(0, 0)
    for row in rows:
        total = total + Count(row["matchCount"], row["winCount"])
    return BreakdownRow(
        group=label,
        matches=total.matches,
        win_rate=total.wins / total.matches if total.matches else None,
        win_rate_ci=tuple(wilson_interval(total.wins, total.matches)) if total.matches else None,
        share_of_hero_games=(total.matches / hero_total) if hero_total else None,
    )


def trend_by_patch(
    rows: list[dict[str, Any]], versions: tuple[GameVersion, ...], patches: int = TREND_PATCHES
) -> list[PatchRate]:
    """Weekly rows summed per patch (assigned by week midpoint), last ``patches`` patches."""
    if not versions:
        return []
    sums: dict[str, Count] = {}
    for row in rows:
        mid = _bucket_start(row) + timedelta(days=3, hours=12)
        live = None
        for v in versions:
            if v.start > mid:
                break
            live = v
        if live is None:
            continue
        sums[live.name] = sums.get(live.name, Count(0, 0)) + Count(
            row["matchCount"], row["winCount"]
        )
    order = [v.name for v in versions if v.name in sums][-patches:]
    earliest = min((_bucket_start(r) for r in rows), default=None)
    result = []
    for name in order:
        count = sums[name]
        idx = next(i for i, v in enumerate(versions) if v.name == name)
        partial = idx == len(versions) - 1 or (
            earliest is not None and earliest > versions[idx].start
        )
        result.append(
            PatchRate(
                patch=name,
                matches=count.matches,
                win_rate=count.wins / count.matches if count.matches else None,
                win_rate_ci=tuple(wilson_interval(count.wins, count.matches))
                if count.matches
                else None,
                partial=partial,
            )
        )
    return result


def win_rate_rank(pool: Pool, hero_id: int, min_sample: int) -> tuple[int | None, int]:
    eligible = sorted(
        ((c.wins / c.matches, hid) for hid, c in pool.counts.items() if c.matches >= min_sample),
        reverse=True,
    )
    ids = [hid for _, hid in eligible]
    return (ids.index(hero_id) + 1 if hero_id in ids else None), len(ids)


async def pro_stats(hero_id: int) -> ProStats | None:
    rows = await _opendota_hero_stats()
    total = round(sum(r.get("pro_pick") or 0 for r in rows) / 10)
    row = next((r for r in rows if r["id"] == hero_id), None)
    if row is None or not total:
        return None
    picks, bans, wins = row.get("pro_pick") or 0, row.get("pro_ban") or 0, row.get("pro_win") or 0
    return ProStats(
        pro_matches=total,
        picks=picks,
        bans=bans,
        pick_rate=picks / total,
        ban_rate=bans / total,
        win_rate=wins / picks if picks else None,
    )


@dataclass(frozen=True, slots=True)
class Analysis[T]:
    result: T
    pool: Pool
    sample_size: int
    sources: list[Source]
    caveats: list[str]


async def hero_overview(hero: Hero, filters: Filters) -> Analysis[HeroOverview]:
    pool = await hero_pool(filters)
    current = hero_rate(pool, hero.id)
    rank, ranked = win_rate_rank(pool, hero.id, filters.min_sample)
    sources: list[Source] = [pool.source]
    caveats = pool_caveats(pool)
    by_position: list[BreakdownRow] = []
    by_bracket: list[BreakdownRow] = []
    trend: list[PatchRate] = []

    if pool.source == "stratz":
        versions = await game_versions()
        window = patch_window(versions, filters.patch)
        assert window is not None
        breakdown = None
        if window.current:
            breakdown = await _query(BREAKDOWN_QUERY, _variables(filters, [hero.id], DAYS_TAKE))
        if breakdown is not None:
            stats = breakdown["heroStats"]
            pos_rows = {
                p: [r for r in stats[f"p_{p.value}"] if _in_window(_bucket_start(r), window)]
                for p in Position
            }
            hero_total = sum(r["matchCount"] for rows in pos_rows.values() for r in rows)
            by_position = [
                _breakdown(p.name.lower(), rows, hero_total) for p, rows in pos_rows.items()
            ]
            by_bracket = [
                _breakdown(
                    b.value,
                    [r for r in stats[f"b_{b.value}"] if _in_window(_bucket_start(r), window)],
                    None,
                )
                for b in RankBracket
            ]
        else:
            caveats.append("Position and medal breakdowns are only computed for the live patch.")
        weekly = await _query(WIN_WEEK_QUERY, _variables(filters, [hero.id], TREND_WEEKS))
        trend = trend_by_patch(weekly["heroStats"]["winWeek"], versions)
        caveats.append(
            "Trend sums STRATZ weekly buckets per lettered patch (each week assigned by its "
            "midpoint); the live patch and the oldest one shown may be partial."
        )
    else:
        rows = await _opendota_hero_stats()
        row = next((r for r in rows if r["id"] == hero.id), {})
        by_bracket = [
            BreakdownRow(
                group=b.value if b.tier < 7 else "divine+immortal",
                matches=row.get(f"{b.tier}_pick") or 0,
                win_rate=(row.get(f"{b.tier}_win") or 0) / row[f"{b.tier}_pick"]
                if row.get(f"{b.tier}_pick")
                else None,
                win_rate_ci=tuple(wilson_interval(row[f"{b.tier}_win"], row[f"{b.tier}_pick"]))
                if row.get(f"{b.tier}_pick")
                else None,
            )
            for b in list(RankBracket)[:7]
        ]
        caveats.append("Position breakdown and patch trend need STRATZ and are omitted.")

    pro = None
    try:
        pro = await pro_stats(hero.id)
    except Exception as exc:  # pro numbers are a bonus; never fail the overview for them
        caveats.append(f"Pro pick/ban rates unavailable ({type(exc).__name__}).")
    if pro is not None:
        if "opendota" not in sources:
            sources.append("opendota")
        caveats.append(
            "Pro pick/ban/win rates come from OpenDota's recent professional matches, not "
            "pub games, and ignore the rank, position and patch filters. Pub ban rates are "
            "not published by either source."
        )

    typical = [
        row.group
        for row in sorted(by_position, key=lambda r: r.matches, reverse=True)
        if (row.share_of_hero_games or 0) >= 0.2
    ]
    overview = HeroOverview(
        hero=hero.display_name,
        hero_id=hero.id,
        roles=list(hero.roles),
        primary_attr=hero.primary_attr,
        attack_type=hero.attack_type,
        patch=pool.patch,
        current=current,
        win_rate_rank=rank,
        ranked_heroes=ranked,
        typical_positions=typical,
        by_position=by_position,
        by_bracket=by_bracket,
        trend=trend,
        pro=pro,
    )
    return Analysis(overview, pool, current.matches, sources, caveats)


METRICS = ("win_rate", "pick_rate", "kda", "xpm", "networth", "networth_per_min", "last_hits")


async def _averages(filters: Filters, hero_ids: list[int]) -> dict[int, dict[str, Any]]:
    brackets, _ = basic_bracket_ids(filters)
    data = await _query(
        HERO_STATS_QUERY,
        {"heroIds": hero_ids, "bracketBasicIds": brackets, "positionIds": position_ids(filters)},
    )
    return {row["heroId"]: row for row in data["heroStats"]["stats"]}


def _average_values(row: dict[str, Any] | None) -> dict[str, float | None]:
    if not row or not row.get("matchCount"):
        return {
            "kda": None,
            "xpm": None,
            "networth": None,
            "networth_per_min": None,
            "last_hits": None,
        }
    minutes = row.get("time") or 0
    deaths = row.get("deaths") or 0
    return {
        "kda": ((row.get("kills") or 0) + (row.get("assists") or 0)) / max(deaths, 1.0),
        "xpm": row["xp"] / minutes if minutes and row.get("xp") is not None else None,
        "networth": row.get("networth"),
        "networth_per_min": row["networth"] / minutes
        if minutes and row.get("networth") is not None
        else None,
        "last_hits": row.get("cs"),
    }


async def compare(
    heroes: list[Hero], filters: Filters, metrics: list[str]
) -> Analysis[ComparisonResult]:
    pool = await hero_pool(filters)
    sources: list[Source] = [pool.source]
    caveats = pool_caveats(pool)
    averages: dict[int, dict[str, Any]] = {}
    needs_averages = any(m not in ("win_rate", "pick_rate") for m in metrics)
    if needs_averages:
        try:
            averages = await _averages(filters, [h.id for h in heroes])
        except Exception as exc:
            if not _stratz_failed(exc):
                raise
            caveats.append("Per-game averages need STRATZ, which is unavailable; they are null.")
        else:
            if "stratz" not in sources:
                sources.append("stratz")
            _, exact = basic_bracket_ids(filters)
            caveats.append(
                "KDA, XPM, net worth and last hits are STRATZ per-game averages over its recent "
                "aggregate window, all game modes, not restricted to the patch filter."
            )
            if not exact:
                caveats.append(
                    "STRATZ averages only exist for paired medals (herald-guardian, "
                    "crusader-archon, legend-ancient, divine-immortal); the pairs overlapping "
                    "your rank filter were used."
                )
    rows = []
    for hero in heroes:
        rate = hero_rate(pool, hero.id)
        values: dict[str, float | None] = {"win_rate": rate.win_rate, "pick_rate": rate.pick_rate}
        values |= _average_values(averages.get(hero.id))
        rows.append((hero, rate, {m: values[m] for m in metrics}))

    first_values = rows[0][2]
    comparisons = []
    for hero, rate, values in rows:
        delta = {
            m: (v - first_values[m]) if v is not None and first_values[m] is not None else None
            for m, v in values.items()
        }
        comparisons.append(
            HeroComparison(
                hero=hero.display_name,
                hero_id=hero.id,
                matches=rate.matches,
                values=values,
                delta_vs_first=delta,
                win_rate_ci=rate.win_rate_ci,
                pick_rate_ci=rate.pick_rate_ci,
            )
        )

    pairs = []
    for (ha, ra, _), (hb, rb, _) in combinations(rows, 2):
        if ra.matches and rb.matches:
            test = two_proportion_ztest(ra.wins, ra.matches, rb.wins, rb.matches)
            pairs.append((ha, hb, ra.win_rate - rb.win_rate, test))
    adjusted = benjamini_hochberg([t.p_value for *_, t in pairs], SIGNIFICANCE) if pairs else None
    tests = [
        PairTest(
            hero_a=ha.display_name,
            hero_b=hb.display_name,
            win_rate_delta=delta,
            z=test.z,
            p_value=test.p_value,
            q_value=adjusted.adjusted[i],
            significant=adjusted.rejected[i],
        )
        for i, (ha, hb, delta, test) in enumerate(pairs)
        if adjusted is not None
    ]
    result = ComparisonResult(
        heroes=comparisons,
        win_rate_tests=tests,
        averages_sample={
            h.display_name: int(averages[h.id]["matchCount"]) for h in heroes if h.id in averages
        },
    )
    sample = min(r.matches for _, r, _ in rows)
    return Analysis(result, pool, sample, sources, caveats)


def assign_tier(edge: float) -> str:
    """Tier from (smoothed win rate - pool win rate); see ``TIER_CUTOFFS``."""
    for tier, cutoff in TIER_CUTOFFS:
        if edge >= cutoff:
            return tier
    return "D"


async def tier_list(filters: Filters, limit: int) -> Analysis[TierList]:
    pool = await hero_pool(filters)
    prior = pool.win_rate
    eligible = [(hid, c) for hid, c in pool.counts.items() if c.matches >= filters.min_sample]
    scored = sorted(
        ((smoothed_rate(c.wins, c.matches, prior, PRIOR_STRENGTH), hid, c) for hid, c in eligible),
        key=lambda t: (-t[0], t[1]),
    )
    entries = [
        TierEntry(
            rank=i + 1,
            tier=assign_tier(score - prior),
            hero=_name(hid),
            hero_id=hid,
            matches=c.matches,
            win_rate=c.wins / c.matches,
            win_rate_ci=tuple(wilson_interval(c.wins, c.matches)),
            smoothed_win_rate=score,
            pick_rate=pool.pick_rate(hid),
        )
        for i, (score, hid, c) in enumerate(scored[:limit])
    ]
    result = TierList(
        pool_win_rate=prior,
        heroes=entries,
        excluded_low_sample=sum(1 for c in pool.counts.values() if c.matches < filters.min_sample),
    )
    return Analysis(result, pool, pool.total_matches, [pool.source], pool_caveats(pool))


async def hidden_gems(filters: Filters, max_pick_rate: float, limit: int) -> Analysis[HiddenGems]:
    pool = await hero_pool(filters)
    prior = pool.win_rate
    total = Count(
        sum(c.matches for c in pool.counts.values()), sum(c.wins for c in pool.counts.values())
    )
    candidates = [
        (hid, c)
        for hid, c in pool.counts.items()
        if c.matches >= filters.min_sample and pool.pick_rate(hid) <= max_pick_rate
    ]
    tests = []
    for hid, c in candidates:
        rest = Count(total.matches - c.matches, total.wins - c.wins)
        p = (
            two_proportion_ztest(c.wins, c.matches, rest.wins, rest.matches).p_value
            if rest.matches
            else 1.0
        )
        tests.append((hid, c, p))
    adjusted = benjamini_hochberg([p for *_, p in tests], SIGNIFICANCE) if tests else None
    gems = []
    for i, (hid, c, p) in enumerate(tests):
        score = smoothed_rate(c.wins, c.matches, prior, PRIOR_STRENGTH)
        if score - prior < GEM_MIN_EDGE:
            continue
        assert adjusted is not None
        gems.append(
            HiddenGem(
                hero=_name(hid),
                hero_id=hid,
                matches=c.matches,
                win_rate=c.wins / c.matches,
                win_rate_ci=tuple(wilson_interval(c.wins, c.matches)),
                smoothed_win_rate=score,
                edge=score - prior,
                pick_rate=pool.pick_rate(hid),
                p_value=p,
                q_value=adjusted.adjusted[i],
                significant=adjusted.rejected[i],
            )
        )
    gems.sort(key=lambda g: (-g.smoothed_win_rate, g.hero_id))
    result = HiddenGems(
        pool_win_rate=prior,
        max_pick_rate=max_pick_rate,
        gems=gems[:limit],
        candidates_checked=len(candidates),
    )
    return Analysis(result, pool, pool.total_matches, [pool.source], pool_caveats(pool))
