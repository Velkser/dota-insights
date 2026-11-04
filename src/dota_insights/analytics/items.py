"""Item statistics: purchase rates, purchase timings, win rate by timing and gold payback.

Sources:

- STRATZ ``heroStats.itemFullPurchase`` gives, per hero, item and purchase minute, how many
  games bought the item at that minute and how many of them were won. ``heroStats.stats`` gives
  the hero's total games under the same filters, which is the denominator of a purchase rate.
  ``itemFullPurchase`` needs a ``heroId``, so all-hero questions query every hero in batches.
- OpenDota ``/scenarios/itemTimings`` is the fallback for one hero's timing win rates.
- OpenDota ``/explorer`` over league (pro) matches gives per-minute ``gold_t`` and the purchase
  log, which is what a gold payback estimate needs.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from pydantic import BaseModel, Field

from dota_insights.analytics.stats import (
    Interval,
    benjamini_hochberg,
    bootstrap_ci,
    two_proportion_ztest,
    wilson_interval,
)
from dota_insights.cache import TTL_AGGREGATE, cached
from dota_insights.models.filters import Filters, RankBracket
from dota_insights.reference import hero_by_id, heroes, patches
from dota_insights.sources import get_opendota, get_stratz

# --- STRATZ filters ------------------------------------------------------------------------

# STRATZ hero aggregates group medals in pairs.
_BASIC_BRACKET = {
    RankBracket.HERALD: "HERALD_GUARDIAN",
    RankBracket.GUARDIAN: "HERALD_GUARDIAN",
    RankBracket.CRUSADER: "CRUSADER_ARCHON",
    RankBracket.ARCHON: "CRUSADER_ARCHON",
    RankBracket.LEGEND: "LEGEND_ANCIENT",
    RankBracket.ANCIENT: "LEGEND_ANCIENT",
    RankBracket.DIVINE: "DIVINE_IMMORTAL",
    RankBracket.IMMORTAL: "DIVINE_IMMORTAL",
}
_PAIR_MEDALS = {
    "HERALD_GUARDIAN": "Herald-Guardian",
    "CRUSADER_ARCHON": "Crusader-Archon",
    "LEGEND_ANCIENT": "Legend-Ancient",
    "DIVINE_IMMORTAL": "Divine-Immortal",
}

STRATZ_LINK_CAVEAT = "Item statistics from STRATZ (https://stratz.com)."
STRATZ_WINDOW_CAVEAT = (
    "STRATZ aggregates cover its recent rolling window of ranked matches; STRATZ does not "
    "report the exact dates, so no period is given."
)
SELECTION_BIAS_CAVEAT = (
    "Item win rates are correlational and biased by selection: players who are already "
    "winning get gold sooner, so they buy items earlier and more often. An early-timing win "
    "rate does not mean that rushing the item causes wins."
)


@dataclass(frozen=True, slots=True)
class StratzScope:
    """How ``Filters`` map onto STRATZ arguments, with caveats for what could not be mapped."""

    brackets: tuple[str, ...] | None
    position: str | None
    caveats: tuple[str, ...]


def stratz_scope(filters: Filters) -> StratzScope:
    caveats: list[str] = []
    brackets: tuple[str, ...] | None = None
    if (rank := filters.rank_range) is not None:
        low, high = rank
        medals = [b for b in RankBracket if low.tier <= b.tier <= high.tier]
        brackets = tuple(dict.fromkeys(_BASIC_BRACKET[m] for m in medals))
        covered = {m for m in RankBracket if _BASIC_BRACKET[m] in brackets}
        if covered != set(medals):
            names = ", ".join(_PAIR_MEDALS[b] for b in brackets)
            caveats.append(
                f"STRATZ groups medals in pairs, so rank {filters.rank_bracket!r} was "
                f"widened to {names}."
            )
    position = f"POSITION_{int(filters.position)}" if filters.position is not None else None
    if filters.patch is not None:
        caveats.append(
            f"STRATZ item aggregates only cover recent matches; the patch filter "
            f"{filters.patch!r} was not applied."
        )
    if filters.game_mode is not None:
        caveats.append(
            "STRATZ item aggregates cannot be filtered by game mode; the game_mode filter "
            "was not applied."
        )
    return StratzScope(brackets, position, tuple(caveats))


# --- STRATZ fetching -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimingRow:
    """Games in which the item was first bought at ``minute`` (whole minutes, STRATZ)."""

    minute: int
    matches: int
    wins: int


@dataclass(frozen=True, slots=True)
class HeroItemStats:
    """One hero's games under the filters and its first purchases of each item by minute."""

    hero_id: int
    matches: int
    wins: int
    purchases: Mapping[int, tuple[TimingRow, ...]] = field(default_factory=dict)

    def rows(self, item_id: int) -> tuple[TimingRow, ...]:
        return self.purchases.get(item_id, ())


HERO_BATCH = 25


def _batch_document(hero_ids: Sequence[int]) -> str:
    args = "bracketBasicIds: $b, positionIds: $p"
    parts = []
    for hero_id in hero_ids:
        parts.append(
            f"h{hero_id}: itemFullPurchase(heroId: {hero_id}, {args}, matchLimit: 0) "
            "{ itemId instance time matchCount winCount }"
        )
        parts.append(f"s{hero_id}: stats(heroIds: [{hero_id}], {args}) {{ matchCount winCount }}")
    return (
        "query($b: [RankBracketBasicEnum], $p: [MatchPlayerPositionType]) "
        "{ heroStats { " + " ".join(parts) + " } }"
    )


def parse_hero_items(data: Mapping[str, Any], hero_ids: Iterable[int]) -> dict[int, HeroItemStats]:
    """Turn a batch response into per-hero stats, keeping first purchases (instance 0) only."""
    stats_root = (data or {}).get("heroStats") or {}
    result = {}
    for hero_id in hero_ids:
        totals = stats_root.get(f"s{hero_id}") or []
        matches = sum(row.get("matchCount") or 0 for row in totals)
        wins = sum(row.get("winCount") or 0 for row in totals)
        purchases: dict[int, dict[int, list[int]]] = {}
        for row in stats_root.get(f"h{hero_id}") or []:
            if (row.get("instance") or 0) != 0 or row.get("time") is None:
                continue
            by_minute = purchases.setdefault(row["itemId"], {})
            counts = by_minute.setdefault(row["time"], [0, 0])
            counts[0] += row.get("matchCount") or 0
            counts[1] += row.get("winCount") or 0
        result[hero_id] = HeroItemStats(
            hero_id=hero_id,
            matches=matches,
            wins=wins,
            purchases={
                item_id: tuple(TimingRow(m, n, w) for m, (n, w) in sorted(by_minute.items()))
                for item_id, by_minute in purchases.items()
            },
        )
    return result


@cached(ttl=TTL_AGGREGATE)
async def _fetch_batch(
    hero_ids: tuple[int, ...], brackets: tuple[str, ...] | None, position: str | None
) -> tuple[dict[int, HeroItemStats], datetime]:
    variables = {"b": list(brackets) if brackets else None, "p": [position] if position else None}
    data = await get_stratz().query(_batch_document(hero_ids), variables)
    return parse_hero_items(data, hero_ids), datetime.now(UTC)


async def fetch_hero_items(
    hero_ids: Iterable[int], scope: StratzScope
) -> tuple[dict[int, HeroItemStats], datetime]:
    """Per-hero item stats for ``hero_ids`` (batched, cached) and when they were fetched."""
    ids = sorted(set(hero_ids))
    batches = [tuple(ids[i : i + HERO_BATCH]) for i in range(0, len(ids), HERO_BATCH)]
    results = await asyncio.gather(
        *(_fetch_batch(batch, scope.brackets, scope.position) for batch in batches)
    )
    merged: dict[int, HeroItemStats] = {}
    for stats, _ in results:
        merged.update(stats)
    fetched = min((when for _, when in results), default=datetime.now(UTC))
    return merged, fetched


def all_hero_ids() -> list[int]:
    return [hero.id for hero in heroes()]


# --- OpenDota fallback -----------------------------------------------------------------------


@cached(ttl=TTL_AGGREGATE)
async def fetch_item_timings(hero_id: int, item_name: str) -> tuple[list[dict], datetime]:
    """OpenDota ``/scenarios/itemTimings`` rows for one hero and item."""
    # OpenDotaClient has no typed method for this endpoint yet.
    rows = await get_opendota()._get(
        "/scenarios/itemTimings", {"hero_id": hero_id, "item": item_name}
    )
    return list(rows or []), datetime.now(UTC)


def opendota_timing_buckets(rows: Iterable[Mapping[str, Any]]) -> list[BucketStat]:
    """Buckets from itemTimings rows: each row covers purchases after the previous threshold."""
    parsed = sorted((int(r["time"]), int(r["games"]), int(r["wins"])) for r in rows)
    buckets = []
    previous = None
    for seconds, games, wins in parsed:
        minute = seconds // 60
        label = f"<{minute}" if previous is None else f"{previous}-{minute}"
        buckets.append(_bucket_stat(label, previous, minute, games, wins))
        previous = minute
    return buckets


# --- Timing buckets and summaries ------------------------------------------------------------


class BucketStat(BaseModel):
    label: str = Field(description='Purchase-minute range, e.g. "<15", "15-20", "25+".')
    from_minute: int | None = Field(description="First purchase minute included (null: open).")
    to_minute: int | None = Field(description="Purchase minutes below this (null: open).")
    games: int = Field(description="Games in which the item was first bought in this range.")
    wins: int
    win_rate: float | None = Field(description="wins / games; null when games is 0.")
    win_rate_ci: tuple[float, float] = Field(description="95% Wilson interval of win_rate.")


def _rate(successes: int, n: int) -> float | None:
    return round(successes / n, 4) if n else None


def rate_ci(successes: int, n: int) -> tuple[float, float]:
    low, high = wilson_interval(successes, n)
    return round(low, 4), round(high, 4)


def _bucket_stat(label: str, start: int | None, end: int | None, n: int, w: int) -> BucketStat:
    return BucketStat(
        label=label,
        from_minute=start,
        to_minute=end,
        games=n,
        wins=w,
        win_rate=_rate(w, n),
        win_rate_ci=rate_ci(w, n),
    )


def bucket_labels(boundaries: Sequence[int]) -> list[tuple[str, int | None, int | None]]:
    """``[15, 20, 25]`` -> ``<15``, ``15-20``, ``20-25``, ``25+`` as (label, start, end)."""
    edges = sorted(set(boundaries))
    if not edges:
        return [("all", None, None)]
    labels = [(f"<{edges[0]}", None, edges[0])]
    labels += [(f"{a}-{b}", a, b) for a, b in zip(edges, edges[1:], strict=False)]
    labels.append((f"{edges[-1]}+", edges[-1], None))
    return labels


def timing_buckets(rows: Iterable[TimingRow], boundaries: Sequence[int]) -> list[BucketStat]:
    """Games and wins by purchase-minute range. A purchase at minute 14 falls in ``<15``."""
    rows = list(rows)
    result = []
    for label, start, end in bucket_labels(boundaries):
        inside = [
            r
            for r in rows
            if (start is None or r.minute >= start) and (end is None or r.minute < end)
        ]
        n = sum(r.matches for r in inside)
        w = sum(r.wins for r in inside)
        result.append(_bucket_stat(label, start, end, n, w))
    return result


def _value_at_rank(rows: Sequence[TimingRow], rank: int) -> int:
    """Minute of the ``rank``-th (1-based) purchase when purchases are sorted by minute."""
    seen = 0
    for row in rows:
        seen += row.matches
        if seen >= rank:
            return row.minute
    return rows[-1].minute


def median_minute(rows: Iterable[TimingRow]) -> tuple[float, tuple[float, float]] | None:
    """Median purchase minute with a distribution-free 95% interval, or None without data.

    For ``n`` purchases sorted by minute the interval runs from order statistic
    ``j = ⌊n/2 - 1.96·√n/2⌋`` to ``k = ⌈n/2 + 1.96·√n/2⌉ + 1`` (clamped to ``[1, n]``), the
    normal approximation to the binomial interval for a median.
    """
    ordered = sorted((r for r in rows if r.matches > 0), key=lambda r: r.minute)
    n = sum(r.matches for r in ordered)
    if n == 0:
        return None
    if n % 2:
        med = float(_value_at_rank(ordered, n // 2 + 1))
    else:
        med = (_value_at_rank(ordered, n // 2) + _value_at_rank(ordered, n // 2 + 1)) / 2
    half = 1.96 * math.sqrt(n) / 2
    j = max(1, math.floor(n / 2 - half))
    k = min(n, math.ceil(n / 2 + half) + 1)
    return med, (float(_value_at_rank(ordered, j)), float(_value_at_rank(ordered, k)))


def quartile_boundaries(rows: Iterable[TimingRow]) -> list[int]:
    """Purchase-minute quartiles, used as default bucket edges so each bucket has data."""
    ordered = sorted((r for r in rows if r.matches > 0), key=lambda r: r.minute)
    n = sum(r.matches for r in ordered)
    if n == 0:
        return []
    edges = {_value_at_rank(ordered, max(1, math.ceil(n * q))) for q in (0.25, 0.5, 0.75)}
    # An edge equal to the first minute would leave the "<edge" bucket empty.
    return sorted(e for e in edges if e > ordered[0].minute)


def merge_rows(groups: Iterable[Iterable[TimingRow]]) -> tuple[TimingRow, ...]:
    by_minute: dict[int, list[int]] = {}
    for rows in groups:
        for r in rows:
            counts = by_minute.setdefault(r.minute, [0, 0])
            counts[0] += r.matches
            counts[1] += r.wins
    return tuple(TimingRow(m, n, w) for m, (n, w) in sorted(by_minute.items()))


def totals(rows: Iterable[TimingRow]) -> tuple[int, int]:
    rows = list(rows)
    return sum(r.matches for r in rows), sum(r.wins for r in rows)


# --- Item overview -----------------------------------------------------------------------------


class HeroItemUsage(BaseModel):
    hero: str
    hero_id: int
    hero_games: int = Field(description="The hero's games under the filters.")
    buyers: int = Field(description="Hero games in which the item was bought.")
    purchase_rate: float = Field(description="buyers / hero_games.")
    purchase_rate_ci: tuple[float, float]
    median_purchase_minute: float | None
    buyer_win_rate: float | None = Field(description="Win rate of the games that bought it.")
    buyer_win_rate_ci: tuple[float, float]


def hero_usage(stats: HeroItemStats, item_id: int) -> HeroItemUsage:
    rows = stats.rows(item_id)
    buyers, wins = totals(rows)
    med = median_minute(rows)
    return HeroItemUsage(
        hero=hero_by_id()[stats.hero_id].display_name,
        hero_id=stats.hero_id,
        hero_games=stats.matches,
        buyers=buyers,
        purchase_rate=_rate(buyers, stats.matches) or 0.0,
        purchase_rate_ci=rate_ci(min(buyers, stats.matches), stats.matches),
        median_purchase_minute=med[0] if med else None,
        buyer_win_rate=_rate(wins, buyers),
        buyer_win_rate_ci=rate_ci(wins, buyers),
    )


# --- Win-rate lift by hero -----------------------------------------------------------------------


class HeroLift(BaseModel):
    hero: str
    hero_id: int
    buyers: int
    buyer_win_rate: float
    buyer_win_rate_ci: tuple[float, float]
    non_buyers: int
    non_buyer_win_rate: float
    lift: float = Field(description="buyer_win_rate - non_buyer_win_rate, in rate points.")
    p_value: float = Field(description="Two-proportion z-test of buyers vs non-buyers.")
    q_value: float = Field(description="Benjamini-Hochberg adjusted p_value across all heroes.")
    significant: bool = Field(description="q_value <= 0.05.")


def hero_lifts(stats: Iterable[HeroItemStats], item_id: int, min_sample: int) -> list[HeroLift]:
    """Buyer vs non-buyer win rate per hero, for heroes with ``min_sample`` games on each side.

    Sorted by ``lift``, largest first; p-values are corrected across every tested hero.
    """
    tested = []
    for hero in stats:
        buyers, wins = totals(hero.rows(item_id))
        buyers, wins = min(buyers, hero.matches), min(wins, hero.wins)
        others, other_wins = hero.matches - buyers, hero.wins - wins
        if buyers < min_sample or others < min_sample:
            continue
        test = two_proportion_ztest(wins, buyers, other_wins, others)
        tested.append((hero, buyers, wins, others, other_wins, test.p_value))
    if not tested:
        return []
    bh = benjamini_hochberg([t[-1] for t in tested])
    lifts = [
        HeroLift(
            hero=hero_by_id()[hero.hero_id].display_name,
            hero_id=hero.hero_id,
            buyers=buyers,
            buyer_win_rate=round(wins / buyers, 4),
            buyer_win_rate_ci=rate_ci(wins, buyers),
            non_buyers=others,
            non_buyer_win_rate=round(other_wins / others, 4),
            lift=round(wins / buyers - other_wins / others, 4),
            p_value=round(p, 6),
            q_value=round(q, 6),
            significant=rejected,
        )
        for (hero, buyers, wins, others, other_wins, p), q, rejected in zip(
            tested, bh.adjusted, bh.rejected, strict=True
        )
    ]
    return sorted(lifts, key=lambda lift: -lift.lift)


# --- Comparing items within timing buckets ---------------------------------------------------


class AdjustedWinRate(BaseModel):
    item: str
    win_rate: float = Field(description="Win rate standardized to the shared timing mix.")
    win_rate_ci: tuple[float, float]


class PairTest(BaseModel):
    bucket: str = Field(description='Timing bucket label, or "timing_adjusted" for the total.')
    item_a: str
    item_b: str
    difference: float = Field(description="Win rate of item_a minus win rate of item_b.")
    p_value: float
    q_value: float = Field(description="Benjamini-Hochberg adjusted across every test here.")
    significant: bool


MIN_TEST_GAMES = 30


def compare_buckets(
    item_rows: Mapping[str, Sequence[TimingRow]], boundaries: Sequence[int]
) -> tuple[dict[str, list[BucketStat]], list[AdjustedWinRate], list[PairTest]]:
    """Per-item bucket stats, timing-standardized win rates and pairwise tests.

    The standardized win rate of item ``i`` is ``Σ_b w_b · p_ib`` where ``w_b`` is the share of
    all compared purchases that fall in bucket ``b``, over buckets every item has games in. Its
    variance is ``Σ_b w_b² · p_ib(1 - p_ib) / n_ib`` (normal approximation). Because every item
    is weighted by the same timing mix, an item bought earlier on average gets no head start.
    Within a bucket, pairs where both items have at least ``MIN_TEST_GAMES`` games get a
    two-proportion z-test; the timing-adjusted pairs use ``z = Δ / √(var_a + var_b)``.
    """
    names = list(item_rows)
    per_item = {name: timing_buckets(item_rows[name], boundaries) for name in names}
    labels = [label for label, _, _ in bucket_labels(boundaries)]
    shared = [i for i in range(len(labels)) if all(per_item[n][i].games > 0 for n in names)]
    shared_total = sum(per_item[n][i].games for n in names for i in shared)

    adjusted: list[AdjustedWinRate] = []
    moments: dict[str, tuple[float, float]] = {}
    if shared_total:
        for name in names:
            rate = var = 0.0
            for i in shared:
                b = per_item[name][i]
                weight = sum(per_item[n][i].games for n in names) / shared_total
                p = b.wins / b.games
                rate += weight * p
                var += weight * weight * p * (1 - p) / b.games
            margin = 1.96 * math.sqrt(var)
            moments[name] = (rate, var)
            adjusted.append(
                AdjustedWinRate(
                    item=name,
                    win_rate=round(rate, 4),
                    win_rate_ci=(
                        round(max(0.0, rate - margin), 4),
                        round(min(1.0, rate + margin), 4),
                    ),
                )
            )

    raw: list[tuple[str, str, str, float, float]] = []
    for a_index, a in enumerate(names):
        for b in names[a_index + 1 :]:
            for i, label in enumerate(labels):
                ba, bb = per_item[a][i], per_item[b][i]
                if ba.games < MIN_TEST_GAMES or bb.games < MIN_TEST_GAMES:
                    continue
                test = two_proportion_ztest(ba.wins, ba.games, bb.wins, bb.games)
                raw.append((label, a, b, ba.wins / ba.games - bb.wins / bb.games, test.p_value))
            if a in moments and b in moments:
                diff = moments[a][0] - moments[b][0]
                se = math.sqrt(moments[a][1] + moments[b][1])
                p = (
                    1.0
                    if se == 0
                    else 2 * (1 - 0.5 * (1 + math.erf(abs(diff / se) / math.sqrt(2))))
                )
                raw.append(("timing_adjusted", a, b, diff, min(1.0, p)))
    tests: list[PairTest] = []
    if raw:
        bh = benjamini_hochberg([r[-1] for r in raw])
        tests = [
            PairTest(
                bucket=label,
                item_a=a,
                item_b=b,
                difference=round(diff, 4),
                p_value=round(p, 6),
                q_value=round(q, 6),
                significant=rejected,
            )
            for (label, a, b, diff, p), q, rejected in zip(
                raw, bh.adjusted, bh.rejected, strict=True
            )
        ]
    return per_item, adjusted, tests


# --- Gold payback (OpenDota explorer, league matches) ------------------------------------------

_ITEM_KEY = re.compile(r"^[a-z0-9_]+$")
_PATCH_MAJOR = re.compile(r"^(\d+\.\d+)")

PAYBACK_WINDOW = 10  # minutes after the purchase
PAYBACK_PRE = 5  # minutes before the purchase, for the difference-in-differences baseline
MIN_CONTROLS = 5


def explorer_patch(filters: Filters) -> str:
    """OpenDota ``match_patch.patch`` value: the major patch ("7.41d" -> "7.41")."""
    if filters.patch:
        match = _PATCH_MAJOR.match(filters.patch)
        if match:
            return match.group(1)
    return patches()[-1].name


def payback_sql(item_key: str, hero_id: int | None, patch: str) -> str:
    """League-match rows with per-minute gold and the item's first purchase time (seconds).

    With a hero, every game of that hero is returned (non-buyers are controls). Without one,
    only games that bought the item, and later buyers serve as controls.
    """
    if not _ITEM_KEY.match(item_key) or not _PATCH_MAJOR.match(patch):
        raise ValueError("unsafe item key or patch for SQL")
    purchase = (
        "(select min((e->>'time')::int) from unnest(pm.purchase_log) e "
        f"where e->>'key' = '{item_key}')"
    )
    where = [f"mp.patch = '{patch}'", "pm.gold_t is not null"]
    if hero_id is not None:
        where.append(f"pm.hero_id = {int(hero_id)}")
    else:
        where.append(f"{purchase} is not null")
    limit = 2000 if hero_id is not None else 3000
    return (
        f"select pm.match_id, pm.hero_id, pm.gold_t, {purchase} as purchase_time "
        "from player_matches pm join match_patch mp using(match_id) "
        f"where {' and '.join(where)} order by pm.match_id desc limit {limit}"
    )


@cached(ttl=TTL_AGGREGATE)
async def fetch_payback_rows(
    item_key: str, hero_id: int | None, patch: str
) -> tuple[list[dict], datetime]:
    data = await get_opendota().explorer(payback_sql(item_key, hero_id, patch))
    if data.get("err"):
        raise RuntimeError(f"OpenDota explorer error: {data['err']}")
    return list(data.get("rows") or []), datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PaybackEstimate:
    games: int  # rows fetched
    buyers: int  # rows that bought the item
    buyers_used: int  # buyers with a full window and enough controls
    median_purchase_minute: float | None
    extra_gpm: float | None
    extra_gpm_ci: Interval | None
    payback_minutes: float | None
    payback_minutes_ci: tuple[float, float] | None


def _gains(gold: Sequence[int], t: int, window: int, pre: int) -> tuple[float, float]:
    """Gold per minute over ``[t, t + window]`` and over ``[t - pre, t]``."""
    return (gold[t + window] - gold[t]) / window, (gold[t] - gold[t - pre]) / pre


def payback_estimate(
    rows: Sequence[Mapping[str, Any]],
    cost: int,
    *,
    window: int = PAYBACK_WINDOW,
    pre: int = PAYBACK_PRE,
    min_controls: int = MIN_CONTROLS,
    seed: int = 0,
) -> PaybackEstimate:
    """Extra gold per minute after buying an item, against games that had not bought it yet.

    For a buyer whose first purchase lands before minute ``t`` (``t = ⌈seconds / 60⌉``),
    controls are games of the same hero that had not bought the item by ``t + window``.
    With ``after = (gold_t[t + window] - gold_t[t]) / window`` and
    ``before = (gold_t[t] - gold_t[t - pre]) / pre``::

        extra_i = (after_i - mean(after_controls)) - (before_i - mean(before_controls))

    so a buyer who was already out-farming the controls before the purchase does not count
    that lead as the item's effect (difference in differences). ``extra_gpm`` is the mean of
    ``extra_i`` with a bootstrap 95% interval, and ``payback = cost / extra_gpm`` minutes,
    reported only when the whole interval is above zero.
    """
    parsed = []
    for row in rows:
        gold = row.get("gold_t") or []
        seconds = row.get("purchase_time")
        parsed.append((row.get("hero_id"), list(gold), seconds))
    buyers = [p for p in parsed if p[2] is not None]
    diffs: list[float] = []
    minutes: list[float] = []
    for hero_id, gold, seconds in buyers:
        minutes.append(seconds / 60)
        t = max(0, math.ceil(seconds / 60))
        if t - pre < 0 or len(gold) <= t + window:
            continue
        controls = [
            g
            for h, g, s in parsed
            if h == hero_id and (s is None or s > (t + window) * 60) and len(g) > t + window
        ]
        if len(controls) < min_controls:
            continue
        after, before = _gains(gold, t, window, pre)
        control_gains = [_gains(g, t, window, pre) for g in controls]
        diffs.append(
            (after - fmean(c[0] for c in control_gains))
            - (before - fmean(c[1] for c in control_gains))
        )
    med = sorted(minutes)[len(minutes) // 2] if minutes else None
    if not diffs:
        return PaybackEstimate(len(parsed), len(buyers), 0, med, None, None, None, None)
    extra = fmean(diffs)
    ci = bootstrap_ci(diffs, stat=fmean, resamples=1000, seed=seed)
    payback = payback_ci = None
    if ci.low > 0:
        payback = round(cost / extra, 1)
        payback_ci = (round(cost / ci.high, 1), round(cost / ci.low, 1))
    return PaybackEstimate(
        games=len(parsed),
        buyers=len(buyers),
        buyers_used=len(diffs),
        median_purchase_minute=round(med, 1) if med is not None else None,
        extra_gpm=round(extra, 1),
        extra_gpm_ci=Interval(round(ci.low, 1), round(ci.high, 1)),
        payback_minutes=payback,
        payback_minutes_ci=payback_ci,
    )
