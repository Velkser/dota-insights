"""Farming, benchmark and power-curve analytics for one hero.

Sources:

- OpenDota ``/benchmarks``: published percentiles (p10..p99) of per-minute stats. OpenDota does
  not say how many matches they rest on.
- OpenDota ``/heroes/{id}/durations``: wins and games per 5-minute match-duration bin.
- OpenDota ``/explorer`` (SQL): per-minute ``gold_t`` / ``xp_t`` / ``lh_t`` arrays and lanes of
  parsed matches. The explorer's ``matches`` table holds league matches only, so these
  numbers describe league play, not ranked pubs.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from statistics import median, quantiles
from typing import Any

from pydantic import BaseModel, Field

from dota_insights.analytics.stats import two_proportion_ztest, wilson_interval
from dota_insights.cache import TTL_AGGREGATE, cached
from dota_insights.models import Filters, Outcome, Position, RankBracket
from dota_insights.reference import hero_by_id, patches
from dota_insights.sources import get_opendota
from dota_insights.sources.opendota import OpenDotaError

# OpenDota benchmark metric -> what it measures.
BENCHMARK_METRICS: dict[str, str] = {
    "gold_per_min": "Gold earned per minute over the whole match.",
    "xp_per_min": "Experience earned per minute over the whole match.",
    "kills_per_min": "Kills per minute.",
    "deaths_per_min": "Deaths per minute.",
    "assists_per_min": "Assists per minute.",
    "last_hits_per_min": "Last hits per minute.",
    "denies_per_min": "Denies per minute.",
    "hero_damage_per_min": "Damage dealt to enemy heroes per minute.",
    "hero_healing_per_min": "Healing done to heroes per minute.",
    "tower_damage": "Total damage dealt to buildings in the match (not per minute).",
}

# Names a caller may use for a metric in ``value``.
METRIC_ALIASES: dict[str, str] = {
    "gpm": "gold_per_min",
    "xpm": "xp_per_min",
    "lh": "last_hits_per_min",
    "lhpm": "last_hits_per_min",
    "lh_per_min": "last_hits_per_min",
    "last_hits": "last_hits_per_min",
    "cs_per_min": "last_hits_per_min",
    "kpm": "kills_per_min",
    "dpm": "hero_damage_per_min",
    "damage_per_min": "hero_damage_per_min",
    "hero_damage": "hero_damage_per_min",
    "healing_per_min": "hero_healing_per_min",
    "denies": "denies_per_min",
}


def metric_name(key: str) -> str | None:
    """Canonical benchmark metric for a caller's key ("gpm" -> "gold_per_min"), or None."""
    text = key.strip().lower().replace(" ", "_").replace("-", "_")
    if text in BENCHMARK_METRICS:
        return text
    return METRIC_ALIASES.get(text)


class Percentile(BaseModel):
    percentile: float = Field(description="Share of matches at or below `value`, 0..1.")
    value: float


class ValuePlacement(BaseModel):
    metric: str
    value: float
    percentile: float = Field(
        description=(
            "Estimated share of matches with a value at or below this one, 0..1, linearly "
            "interpolated between the published percentiles."
        )
    )
    label: str = Field(description='Human-readable placement, e.g. "p63" or "below p10".')


class HeroBenchmarks(BaseModel):
    hero: str
    hero_id: int
    bracket: str | None = Field(description="Medal the percentiles are for; null for all.")
    role: str | None = Field(description='"core" (positions 1-3), "support" (4-5) or null.')
    metrics: dict[str, list[Percentile]]
    placements: list[ValuePlacement] = Field(default_factory=list)


def benchmark_params(filters: Filters) -> tuple[int | None, str | None]:
    """OpenDota ``bracket`` (medal tier 1..7) and ``role`` for ``filters``.

    Raises ValueError for rank ranges and Immortal, which ``/benchmarks`` cannot serve.
    """
    bracket: int | None = None
    if filters.rank_range is not None:
        low, high = filters.rank_range
        if low is not high:
            raise ValueError(
                "OpenDota benchmarks are published per single medal; pass one medal "
                f'such as "{high.value}" instead of a range'
            )
        if low is RankBracket.IMMORTAL:
            raise ValueError("OpenDota publishes no Immortal benchmarks; use divine instead")
        bracket = low.tier
    role = None
    if filters.position is not None:
        role = "core" if filters.position <= Position.OFFLANE else "support"
    return bracket, role


@cached(ttl=TTL_AGGREGATE)
async def fetch_benchmarks(hero_id: int, bracket: int | None, role: str | None) -> dict[str, Any]:
    params = {"hero_id": hero_id, "bracket": bracket, "role": role}
    # OpenDotaClient.benchmarks() takes no bracket/role, so call the endpoint directly.
    return await get_opendota()._get("/benchmarks", params)


def parse_benchmarks(payload: Mapping[str, Any]) -> dict[str, list[Percentile]]:
    """Percentile lists per metric, sorted, with null values dropped."""
    metrics: dict[str, list[Percentile]] = {}
    for name, rows in (payload.get("result") or {}).items():
        points = [
            Percentile(percentile=row["percentile"], value=row["value"])
            for row in rows or []
            if row.get("value") is not None and row.get("percentile") is not None
        ]
        if points:
            metrics[name] = sorted(points, key=lambda p: p.percentile)
    return metrics


def place_value(points: Sequence[Percentile], value: float) -> tuple[float, str]:
    """Where ``value`` falls among published percentiles: (estimated percentile, label).

    Between two published points the percentile is linearly interpolated. Below the lowest
    point or above the highest, the estimate is clamped to that point and the label says
    "below pNN" / "above pNN".
    """
    first, last = points[0], points[-1]
    if value < first.value:
        return first.percentile, f"below p{round(first.percentile * 100)}"
    if value > last.value:
        return last.percentile, f"above p{round(last.percentile * 100)}"
    for lo, hi in zip(points, points[1:], strict=False):
        if lo.value <= value <= hi.value:
            if hi.value == lo.value:
                pct = hi.percentile
            else:
                frac = (value - lo.value) / (hi.value - lo.value)
                pct = lo.percentile + frac * (hi.percentile - lo.percentile)
            return pct, f"p{round(pct * 100)}"
    return last.percentile, f"p{round(last.percentile * 100)}"


# Power curve

# Bucket edges in minutes: [0, 20), [20, 25), ..., [50, ∞).
DURATION_EDGES = (0, 20, 25, 30, 35, 40, 45, 50)
EARLY_BEFORE_MIN = 30
LATE_FROM_MIN = 40
POWER_ALPHA = 0.05
MIN_BUCKET_GAMES = 30


class DurationBucket(BaseModel):
    label: str = Field(description='Match-duration range in minutes, e.g. "25-30" or "50+".')
    from_min: int
    to_min: int | None
    games: int
    wins: int
    win_rate: float
    ci_low: float
    ci_high: float


class PowerCurve(BaseModel):
    hero: str
    hero_id: int
    buckets: list[DurationBucket]
    peak: str | None = Field(
        description="Bucket with the highest win rate among buckets with enough games."
    )
    early_win_rate: float | None
    late_win_rate: float | None
    classification: str = Field(description='"early", "late" or "flat" (see definitions).')
    p_value: float | None = Field(description="Two-proportion z-test p-value, late vs early.")


@cached(ttl=TTL_AGGREGATE)
async def fetch_durations(hero_id: int) -> list[dict[str, Any]]:
    # OpenDotaClient has no method for this endpoint yet.
    return await get_opendota()._get(f"/heroes/{hero_id}/durations")


def _bucket_index(minutes: float) -> int:
    index = 0
    for i, edge in enumerate(DURATION_EDGES):
        if minutes >= edge:
            index = i
    return index


def bucket_durations(rows: Sequence[Mapping[str, Any]]) -> list[DurationBucket]:
    """Sum 5-minute OpenDota bins (``duration_bin`` in seconds) into ``DURATION_EDGES``."""
    games = [0] * len(DURATION_EDGES)
    wins = [0] * len(DURATION_EDGES)
    for row in rows:
        i = _bucket_index(int(row["duration_bin"]) / 60)
        games[i] += int(row["games_played"])
        wins[i] += int(row["wins"])
    buckets = []
    for i, start in enumerate(DURATION_EDGES):
        if games[i] == 0:
            continue
        end = DURATION_EDGES[i + 1] if i + 1 < len(DURATION_EDGES) else None
        label = f"{start}-{end}" if end is not None else f"{start}+"
        if start == 0:
            label = f"<{end}"
        ci = wilson_interval(wins[i], games[i])
        buckets.append(
            DurationBucket(
                label=label,
                from_min=start,
                to_min=end,
                games=games[i],
                wins=wins[i],
                win_rate=wins[i] / games[i],
                ci_low=ci.low,
                ci_high=ci.high,
            )
        )
    return buckets


def power_curve(hero: str, hero_id: int, buckets: list[DurationBucket]) -> PowerCurve:
    """Peak window and early/late classification.

    Early = matches shorter than ``EARLY_BEFORE_MIN`` minutes, late = ``LATE_FROM_MIN`` and
    longer. "late" if the late win rate is higher and a pooled two-proportion z-test gives
    p < ``POWER_ALPHA``; "early" if lower with p < ``POWER_ALPHA``; otherwise "flat".
    """
    eligible = [b for b in buckets if b.games >= MIN_BUCKET_GAMES]
    peak = max(eligible, key=lambda b: b.win_rate).label if eligible else None
    early = [b for b in buckets if b.to_min is not None and b.to_min <= EARLY_BEFORE_MIN]
    late = [b for b in buckets if b.from_min >= LATE_FROM_MIN]
    ew, en = sum(b.wins for b in early), sum(b.games for b in early)
    lw, ln = sum(b.wins for b in late), sum(b.games for b in late)
    classification, p_value = "flat", None
    if en and ln:
        test = two_proportion_ztest(lw, ln, ew, en)
        p_value = test.p_value
        if p_value < POWER_ALPHA:
            classification = "late" if test.z > 0 else "early"
    return PowerCurve(
        hero=hero,
        hero_id=hero_id,
        buckets=buckets,
        peak=peak,
        early_win_rate=ew / en if en else None,
        late_win_rate=lw / ln if ln else None,
        classification=classification,
        p_value=p_value,
    )


# Per-minute farm from parsed league matches (OpenDota explorer)

LEAGUE_MATCH_LIMIT = 300
MIN_CURVE_GAMES = 20
MIN_PAIR_GAMES = 10
LANE_MINUTE = 10
LANE_MARGIN = 0.10
_PATCH_RE = re.compile(r"^(\d+\.\d+)[a-z]?$")


def explorer_patch(filters: Filters) -> str:
    """Major patch name for the explorer's ``match_patch`` table ("7.41f" -> "7.41").

    Omitted patch means the newest patch in the reference data. Raises ValueError for text
    that is not a patch name, so nothing else reaches the SQL.
    """
    if filters.patch is None:
        return patches()[-1].name
    match = _PATCH_RE.match(filters.patch.strip().lower())
    if not match:
        raise ValueError(f'{filters.patch!r} is not a patch name such as "7.41" or "7.41f"')
    return match.group(1)


def farm_sql(hero_id: int, patch: str, limit: int = LEAGUE_MATCH_LIMIT) -> str:
    """Newest parsed league matches of ``hero_id`` on ``patch`` with per-minute arrays."""
    return (
        "SELECT pm.match_id, pm.player_slot, m.radiant_win, m.start_time, "
        "pm.gold_t, pm.xp_t, pm.lh_t "
        "FROM player_matches pm JOIN matches m USING (match_id) "
        "JOIN match_patch mp USING (match_id) "
        f"WHERE pm.hero_id = {int(hero_id)} AND pm.gold_t IS NOT NULL "
        f"AND mp.patch = '{patch}' "
        f"ORDER BY pm.match_id DESC LIMIT {int(limit)}"
    )


def lane_sql(hero_id: int, patch: str, limit: int = LEAGUE_MATCH_LIMIT) -> str:
    """All ten players' lane and farm at minute ``LANE_MINUTE`` in ``hero_id``'s matches."""
    i = LANE_MINUTE + 1  # Postgres arrays are 1-based; index 1 is minute 0.
    return (
        "SELECT pm.match_id, pm.player_slot, pm.hero_id, pm.lane, pm.is_roaming, "
        f"pm.gold_t[{i}] AS gold_10, pm.xp_t[{i}] AS xp_10, pm.lh_t[{i}] AS lh_10, "
        "m.radiant_win, m.start_time "
        "FROM player_matches pm JOIN matches m USING (match_id) "
        "WHERE pm.match_id IN (SELECT p.match_id FROM player_matches p "
        "JOIN match_patch mp USING (match_id) "
        f"WHERE p.hero_id = {int(hero_id)} AND p.gold_t IS NOT NULL "
        f"AND mp.patch = '{patch}' ORDER BY p.match_id DESC LIMIT {int(limit)})"
    )


@cached(ttl=TTL_AGGREGATE)
async def fetch_explorer(sql: str) -> list[dict[str, Any]]:
    payload = await get_opendota().explorer(sql)
    if payload.get("err"):
        raise OpenDotaError(f"OpenDota explorer query failed: {payload['err']}")
    return payload.get("rows") or []


def _won(row: Mapping[str, Any]) -> bool:
    return (row["player_slot"] < 128) == bool(row["radiant_win"])


def filter_outcome(rows: Sequence[Mapping[str, Any]], outcome: Outcome) -> list[Mapping[str, Any]]:
    if outcome is Outcome.ALL:
        return list(rows)
    want = outcome is Outcome.WIN
    return [r for r in rows if _won(r) == want]


def match_period(rows: Sequence[Mapping[str, Any]]) -> tuple[date, date] | None:
    times = [r["start_time"] for r in rows if r.get("start_time")]
    if not times:
        return None
    return _day(min(times)), _day(max(times))


def newest_match(rows: Sequence[Mapping[str, Any]]) -> datetime | None:
    times = [r["start_time"] for r in rows if r.get("start_time")]
    return datetime.fromtimestamp(max(times), UTC) if times else None


def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, UTC).date()


class Spread(BaseModel):
    median: float
    q1: float = Field(description="25th percentile.")
    q3: float = Field(description="75th percentile.")


def spread(values: Sequence[float]) -> Spread:
    """Median and quartiles (``statistics.quantiles``, inclusive method)."""
    if len(values) == 1:
        v = float(values[0])
        return Spread(median=v, q1=v, q3=v)
    q1, mid, q3 = quantiles(values, n=4, method="inclusive")
    return Spread(median=mid, q1=q1, q3=q3)


class MinuteFarm(BaseModel):
    minute: int
    games: int = Field(description="Matches that lasted at least this long.")
    gold: Spread | None = Field(description="OpenDota gold_t at this minute; null if too few.")
    gpm: Spread | None
    xp: Spread | None
    xpm: Spread | None
    last_hits: Spread | None


class FarmingStats(BaseModel):
    hero: str
    hero_id: int
    patch: str
    outcome: str
    minutes: list[MinuteFarm]


def farm_at_minutes(
    rows: Sequence[Mapping[str, Any]], minutes: Sequence[int], min_games: int
) -> list[MinuteFarm]:
    """Spread of gold, GPM, XP, XPM and last hits at each minute over ``rows``."""
    out = []
    for m in minutes:
        alive = [r for r in rows if len(r["gold_t"]) > m]
        if len(alive) < min_games:
            out.append(MinuteFarm(minute=m, games=len(alive), gold=None, gpm=None, xp=None,
                                  xpm=None, last_hits=None))  # fmt: skip
            continue
        gold = [r["gold_t"][m] for r in alive]
        xp = [r["xp_t"][m] for r in alive]
        out.append(
            MinuteFarm(
                minute=m,
                games=len(alive),
                gold=spread(gold),
                gpm=spread([g / m for g in gold]) if m else None,
                xp=spread(xp),
                xpm=spread([x / m for x in xp]) if m else None,
                last_hits=spread([r["lh_t"][m] for r in alive]),
            )
        )
    return out


class CurvePoint(BaseModel):
    minute: int
    games: int
    gold_median: float
    xp_median: float


class HeroCurves(BaseModel):
    hero: str
    hero_id: int
    games: int
    wins: list[CurvePoint] | None = Field(description="Matches the hero won; null if excluded.")
    losses: list[CurvePoint] | None = Field(description="Matches the hero lost; null if excluded.")


def median_curve(rows: Sequence[Mapping[str, Any]], min_games: int = MIN_CURVE_GAMES):
    """Median gold_t and xp_t per minute, while at least ``min_games`` matches last."""
    points = []
    minute = 0
    while True:
        alive = [r for r in rows if len(r["gold_t"]) > minute]
        if len(alive) < min_games:
            return points
        points.append(
            CurvePoint(
                minute=minute,
                games=len(alive),
                gold_median=median(r["gold_t"][minute] for r in alive),
                xp_median=median(r["xp_t"][minute] for r in alive),
            )
        )
        minute += 1


# Laning


class LaneMatchup(BaseModel):
    hero: str
    hero_id: int
    games: int = Field(description="Laning phases with this hero in the same lane.")
    lane_win_rate: float
    lane_loss_rate: float


class LaneStats(BaseModel):
    hero: str
    hero_id: int
    patch: str
    games: int = Field(description="Matches where the hero laned against at least one enemy.")
    skipped: int = Field(description="Matches left out: roaming, no lane data or empty lane.")
    lane_win_rate: float
    lane_draw_rate: float
    lane_loss_rate: float
    match_win_rate_after_lane_win: float | None
    match_win_rate_after_lane_loss: float | None
    gold_10: Spread
    last_hits_10: Spread
    best_opponents: list[LaneMatchup] = Field(description="Enemies the hero beats most often.")
    worst_opponents: list[LaneMatchup] = Field(description="Enemies that beat the hero most.")
    best_partners: list[LaneMatchup]
    worst_partners: list[LaneMatchup]


def lane_result(own: Sequence[float], enemy: Sequence[float]) -> str:
    """ "win", "draw" or "loss" from average gold per hero on each side of the lane.

    ``edge = (own_avg - enemy_avg) / ((own_avg + enemy_avg) / 2)``; win if
    ``edge >= LANE_MARGIN``, loss if ``edge <= -LANE_MARGIN``, else draw.
    """
    a, b = sum(own) / len(own), sum(enemy) / len(enemy)
    if a + b == 0:
        return "draw"
    edge = (a - b) / ((a + b) / 2)
    if edge >= LANE_MARGIN:
        return "win"
    if edge <= -LANE_MARGIN:
        return "loss"
    return "draw"


def lane_stats(
    hero_name: str, hero_id: int, patch: str, rows: Sequence[Mapping[str, Any]]
) -> LaneStats:
    by_match: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_match[row["match_id"]].append(row)
    counts = Counter()
    after = Counter()
    gold, lhs = [], []
    opp: dict[int, Counter] = defaultdict(Counter)
    mate: dict[int, Counter] = defaultdict(Counter)
    skipped = 0
    for players in by_match.values():
        me = next((p for p in players if p["hero_id"] == hero_id), None)
        if me is None or me["is_roaming"] or me["lane"] is None or me["gold_10"] is None:
            skipped += 1
            continue
        radiant = me["player_slot"] < 128
        same = [p for p in players if p["lane"] == me["lane"] and p["gold_10"] is not None
                and not p["is_roaming"]]  # fmt: skip
        allies = [p for p in same if (p["player_slot"] < 128) == radiant]
        enemies = [p for p in same if (p["player_slot"] < 128) != radiant]
        if not enemies:
            skipped += 1
            continue
        result = lane_result([p["gold_10"] for p in allies], [p["gold_10"] for p in enemies])
        counts[result] += 1
        after[(result, _won(me))] += 1
        gold.append(me["gold_10"])
        lhs.append(me["lh_10"] or 0)
        for p in enemies:
            opp[p["hero_id"]][result] += 1
        for p in allies:
            if p is not me:
                mate[p["hero_id"]][result] += 1
    n = sum(counts.values())

    def matchups(table: dict[int, Counter]) -> list[LaneMatchup]:
        heroes_by_id = hero_by_id()
        out = []
        for other, c in table.items():
            games = sum(c.values())
            if games >= MIN_PAIR_GAMES and other in heroes_by_id:
                out.append(LaneMatchup(hero=heroes_by_id[other].display_name, hero_id=other,
                                       games=games, lane_win_rate=c["win"] / games,
                                       lane_loss_rate=c["loss"] / games))  # fmt: skip
        return out

    def ranked(items: list[LaneMatchup], best: bool) -> list[LaneMatchup]:
        # Net lane result (win rate minus loss rate); more games first on ties.
        sign = 1 if best else -1
        return sorted(items, key=lambda m: (-sign * (m.lane_win_rate - m.lane_loss_rate),
                                             -m.games))[:5]  # fmt: skip

    def rate(result: str) -> float | None:
        total = after[(result, True)] + after[(result, False)]
        return after[(result, True)] / total if total else None

    opponents, partners = matchups(opp), matchups(mate)
    return LaneStats(
        hero=hero_name,
        hero_id=hero_id,
        patch=patch,
        games=n,
        skipped=skipped,
        lane_win_rate=counts["win"] / n if n else 0.0,
        lane_draw_rate=counts["draw"] / n if n else 0.0,
        lane_loss_rate=counts["loss"] / n if n else 0.0,
        match_win_rate_after_lane_win=rate("win"),
        match_win_rate_after_lane_loss=rate("loss"),
        gold_10=spread(gold) if gold else Spread(median=0, q1=0, q3=0),
        last_hits_10=spread(lhs) if lhs else Spread(median=0, q1=0, q3=0),
        best_opponents=ranked(opponents, best=True),
        worst_opponents=ranked(opponents, best=False),
        best_partners=ranked(partners, best=True),
        worst_partners=ranked(partners, best=False),
    )
