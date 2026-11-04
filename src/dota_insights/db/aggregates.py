"""Read helpers over the aggregate tables: hero stats per patch and before/after comparisons.

Filters merge buckets by summing counters, so results always carry their true sample size.
``rank_brackets`` / ``positions`` of None mean all of them. Rates are None when the sample is
empty. ``stale`` says the patch's aggregates are waiting for a rebuild (see
``collector.patches``), so numbers may still include matches from a neighbouring patch.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dota_insights.db.models import (
    HISTOGRAM_BUCKET_WIDTH,
    AggregateStatus,
    HeroAggregate,
    HeroDraftAggregate,
    HeroStatHistogram,
    HistogramStat,
    Patch,
    PatchBracketTotal,
)

PERCENTILES = (0.25, 0.5, 0.75)


def percentile_from_histogram(buckets: Mapping[int, int], q: float, width: int) -> float | None:
    """The ``q`` quantile (0-1), interpolating linearly inside the bucket that contains it."""
    total = sum(buckets.values())
    if total <= 0:
        return None
    target = q * total
    seen = 0
    for lower in sorted(buckets):
        count = buckets[lower]
        if count and seen + count >= target:
            return lower + width * (target - seen) / count
        seen += count
    return float(max(buckets) + width)  # pragma: no cover - unreachable for 0 <= q <= 1


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


@dataclass
class HeroPatchStats:
    hero_id: int
    patch_id: int
    stale: bool
    # Draft level: every collected match.
    matches: int
    games: int
    wins: int
    win_rate: float | None
    pick_rate: float | None
    # Ban rate only from matches whose bans are known (public bans are hidden by the sources).
    ban_sample_matches: int
    ban_rate: float | None
    # Detail level: enriched matches, filtered by position.
    detailed_games: int
    detailed_win_rate: float | None
    averages: dict[str, float | None] = field(default_factory=dict)
    percentiles: dict[str, dict[str, float | None]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _in(column, values: Sequence[int] | None):
    return [] if values is None else [column.in_(list(values))]


def hero_patch_stats(
    session: Session,
    hero_id: int,
    patch_id: int,
    *,
    rank_brackets: Sequence[int] | None = None,
    positions: Sequence[int] | None = None,
) -> HeroPatchStats:
    totals = session.execute(
        select(
            func.coalesce(func.sum(PatchBracketTotal.games), 0),
            func.coalesce(func.sum(PatchBracketTotal.ban_sample_games), 0),
        ).where(
            PatchBracketTotal.patch_id == patch_id,
            *_in(PatchBracketTotal.rank_bracket, rank_brackets),
        )
    ).one()
    draft = session.execute(
        select(
            func.coalesce(func.sum(HeroDraftAggregate.games), 0),
            func.coalesce(func.sum(HeroDraftAggregate.wins), 0),
            func.coalesce(func.sum(HeroDraftAggregate.bans), 0),
        ).where(
            HeroDraftAggregate.patch_id == patch_id,
            HeroDraftAggregate.hero_id == hero_id,
            *_in(HeroDraftAggregate.rank_bracket, rank_brackets),
        )
    ).one()
    sums = ("sum_kills", "sum_deaths", "sum_assists", "sum_gpm", "sum_xpm", "sum_last_hits",
            "sum_net_worth", "sum_hero_damage", "sum_duration")  # fmt: skip
    detail = session.execute(
        select(
            func.coalesce(func.sum(HeroAggregate.games), 0),
            func.coalesce(func.sum(HeroAggregate.wins), 0),
            *(func.coalesce(func.sum(getattr(HeroAggregate, s)), 0) for s in sums),
        ).where(
            HeroAggregate.patch_id == patch_id,
            HeroAggregate.hero_id == hero_id,
            *_in(HeroAggregate.rank_bracket, rank_brackets),
            *_in(HeroAggregate.position, positions),
        )
    ).one()
    detailed_games, detailed_wins = detail[0], detail[1]
    averages = {
        name.removeprefix("sum_"): _ratio(value, detailed_games)
        for name, value in zip(sums, detail[2:], strict=True)
    }
    averages["duration_minutes"] = (
        averages.pop("duration") / 60 if averages["duration"] is not None else None
    )

    histogram_rows = session.execute(
        select(HeroStatHistogram.stat, HeroStatHistogram.bucket, func.sum(HeroStatHistogram.games))
        .where(
            HeroStatHistogram.patch_id == patch_id,
            HeroStatHistogram.hero_id == hero_id,
            *_in(HeroStatHistogram.rank_bracket, rank_brackets),
            *_in(HeroStatHistogram.position, positions),
        )
        .group_by(HeroStatHistogram.stat, HeroStatHistogram.bucket)
    )
    histograms: dict[HistogramStat, dict[int, int]] = {stat: {} for stat in HistogramStat}
    for stat, bucket, games in histogram_rows:
        histograms[HistogramStat(stat)][bucket] = games
    percentiles = {
        stat.value: {
            f"p{round(q * 100)}": percentile_from_histogram(
                histograms[stat], q, HISTOGRAM_BUCKET_WIDTH[stat]
            )
            for q in PERCENTILES
        }
        for stat in HistogramStat
    }

    stale = bool(
        session.scalar(select(AggregateStatus.stale).where(AggregateStatus.patch_id == patch_id))
    )
    matches, ban_sample = totals
    games, wins, bans = draft
    return HeroPatchStats(
        hero_id=hero_id,
        patch_id=patch_id,
        stale=stale,
        matches=matches,
        games=games,
        wins=wins,
        win_rate=_ratio(wins, games),
        pick_rate=_ratio(games, matches),
        ban_sample_matches=ban_sample,
        ban_rate=_ratio(bans, ban_sample),
        detailed_games=detailed_games,
        detailed_win_rate=_ratio(detailed_wins, detailed_games),
        averages=averages,
        percentiles=percentiles,
    )


def latest_patch_ids(session: Session, count: int = 2) -> list[int]:
    """The ``count`` most recently released patch ids, newest first."""
    return list(
        session.scalars(select(Patch.patch_id).order_by(Patch.released_at.desc()).limit(count))
    )


def _delta(before: float | None, after: float | None) -> float | None:
    return None if before is None or after is None else after - before


def compare_hero_patches(
    session: Session,
    hero_id: int,
    before_patch_id: int | None = None,
    after_patch_id: int | None = None,
    *,
    rank_brackets: Sequence[int] | None = None,
    positions: Sequence[int] | None = None,
) -> dict:
    """Before/after comparison of one hero across two patches (default: the latest two).

    Returns ``{"before": {...}, "after": {...}, "delta": {...}}``; deltas are after - before for
    rates and averages, and None where either side has no sample.
    """
    if before_patch_id is None or after_patch_id is None:
        latest = latest_patch_ids(session, 2)
        if len(latest) < 2:
            raise ValueError("need two registered patches to compare")
        after_patch_id = after_patch_id if after_patch_id is not None else latest[0]
        before_patch_id = before_patch_id if before_patch_id is not None else latest[1]
    kwargs = {"rank_brackets": rank_brackets, "positions": positions}
    before = hero_patch_stats(session, hero_id, before_patch_id, **kwargs)
    after = hero_patch_stats(session, hero_id, after_patch_id, **kwargs)
    delta: dict[str, float | None] = {
        name: _delta(getattr(before, name), getattr(after, name))
        for name in ("win_rate", "pick_rate", "ban_rate", "detailed_win_rate")
    }
    for name in after.averages:
        delta[f"avg_{name}"] = _delta(before.averages.get(name), after.averages[name])
    for stat, values in after.percentiles.items():
        delta[f"{stat}_p50"] = _delta(before.percentiles[stat]["p50"], values["p50"])
    return {"before": before.as_dict(), "after": after.as_dict(), "delta": delta}
