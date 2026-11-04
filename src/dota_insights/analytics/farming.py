"""Farming, benchmark and power-curve analytics for one hero.

Sources:

- OpenDota ``/benchmarks``: published percentiles (p10..p99) of per-minute stats. OpenDota does
  not say how many matches they rest on.
- OpenDota ``/heroes/{id}/durations``: wins and games per 5-minute match-duration bin.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from dota_insights.analytics.stats import two_proportion_ztest, wilson_interval
from dota_insights.cache import TTL_AGGREGATE, cached
from dota_insights.models import Filters, Position, RankBracket
from dota_insights.sources import get_opendota

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
