"""Item tools: overview, timing-dependent effectiveness, item comparisons and gold payback."""

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import items as analytics
from dota_insights.mcp.envelope import ToolResult, error, insufficient_data, ok
from dota_insights.mcp.errors import retry_hint, source_error
from dota_insights.models.filters import Filters
from dota_insights.reference import Hero, Item, resolve_hero, resolve_item
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

ItemName = Annotated[
    str,
    Field(
        description=(
            'Item name as a person would type it, in English or Russian: "Black King Bar", '
            '"bkb", "линка", "battle fury".'
        )
    ),
]
HeroName = Annotated[
    str | None,
    Field(
        description=('Hero name or slang, e.g. "Anti-Mage", "am", "Фантомка". Omit for all heroes.')
    ),
]
FiltersArg = Annotated[
    Filters | None,
    Field(
        description=(
            "Match filters. rank_bracket and position are applied. patch and game_mode are "
            "applied only when the collector's database answers (major patch, ranked); STRATZ "
            "item aggregates ignore them and say so in caveats."
        )
    ),
]
TimingBuckets = Annotated[
    list[Annotated[int, Field(ge=1, le=120)]] | None,
    Field(
        description=(
            "Purchase-minute boundaries for timing buckets, e.g. [15, 20, 25] gives <15, "
            "15-20, 20-25 and 25+. Omit to use the item's purchase-time quartiles."
        ),
        max_length=8,
    ),
]
TopHeroes = Annotated[int, Field(ge=1, le=30, description="How many heroes to list.")]

TIMING_DEFINITION = (
    "Games in which the item was first bought within the purchase-minute range (whole "
    "minutes as reported by STRATZ; a purchase at 14:59 is in <15), the share of them won, "
    "and a 95% Wilson interval."
)


def _resolve_item(query: str) -> Item | ToolResult:
    resolution = resolve_item(query)
    if resolution.best is not None:
        return resolution.best
    if not resolution.found:
        return error(f"No item matches {query!r}.", hint="Check the spelling or use the full name.")
    names = ", ".join(c.record.display_name or c.record.name for c in resolution.candidates)
    return error(f"{query!r} is ambiguous: {names}.", hint="Repeat the call with one of these.")


def _resolve_hero(query: str) -> Hero | ToolResult:
    resolution = resolve_hero(query)
    if resolution.best is not None:
        return resolution.best
    if not resolution.found:
        return error(f"No hero matches {query!r}.", hint="Check the spelling or use the full name.")
    names = ", ".join(c.record.display_name for c in resolution.candidates)
    return error(f"{query!r} is ambiguous: {names}.", hint="Repeat the call with one of these.")


def _item_label(item: Item) -> str:
    return item.display_name or item.name


def _widen_hint(filters: Filters) -> str:
    if filters.position is not None or filters.rank_bracket is not None:
        return "Drop position or widen rank_bracket (e.g. legend+), or lower min_sample."
    return "Lower min_sample, or ask about a hero that buys this item more often."


def _stratz_error(exc: StratzError, filters: Filters) -> ToolResult:
    return source_error(exc, filters=filters, source=["stratz"])


def _bucket_confidence(prefix: str, buckets: list[analytics.BucketStat]) -> dict:
    return {f"{prefix}win_rate[{b.label}]": b.win_rate_ci for b in buckets if b.games}


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_item_overview(
        item: ItemName, filters: FiltersArg = None, top_heroes: TopHeroes = 10
    ) -> ToolResult[dict[str, Any]]:
        """How often an item is bought, by which heroes, when, and win rate by purchase time.

        Covers every hero: overall purchase rate, median purchase minute, win rate by timing
        quartile, and the heroes that buy it most often. Win rates are correlational.
        """
        filters = filters or Filters()
        resolved = _resolve_item(item)
        if isinstance(resolved, ToolResult):
            return resolved
        try:
            data = await analytics.load_hero_items(analytics.all_hero_ids(), filters, [resolved.id])
        except StratzError as exc:
            return _stratz_error(exc, filters)
        stats, fetched = data.stats, data.fetched

        rows = analytics.merge_rows(h.rows(resolved.id) for h in stats.values())
        buyers, wins = analytics.totals(rows)
        caveats = [analytics.SELECTION_BIAS_CAVEAT, *data.caveats]
        if buyers < filters.min_sample:
            return insufficient_data(
                f"Only {buyers} games bought {_item_label(resolved)} under these filters.",
                _widen_hint(filters),
                filters=filters,
                sample_size=buyers,
                caveats=caveats,
                source=data.sources,
                period=data.period,
                data_freshness=fetched,
            )
        # Each hero game is one player-game, so this is the share of player-games that buy it.
        player_games = sum(h.matches for h in stats.values())
        median = analytics.median_minute(rows)
        buckets = analytics.timing_buckets(rows, analytics.quartile_boundaries(rows))
        usage = [
            analytics.hero_usage(h, resolved.id)
            for h in stats.values()
            if h.matches >= filters.min_sample and h.rows(resolved.id)
        ]
        usage.sort(key=lambda u: -u.purchase_rate)
        result = {
            "item": _item_label(resolved),
            "item_id": resolved.id,
            "cost": resolved.cost,
            "buyers": buyers,
            "player_games": player_games,
            "purchase_rate": round(buyers / player_games, 4) if player_games else None,
            "median_purchase_minute": median[0] if median else None,
            "buyer_win_rate": round(wins / buyers, 4),
            "win_rate_by_timing": [b.model_dump() for b in buckets],
            "top_heroes_by_purchase_rate": [u.model_dump() for u in usage[:top_heroes]],
        }
        confidence = {
            "purchase_rate": analytics.rate_ci(min(buyers, player_games), player_games),
            "buyer_win_rate": analytics.rate_ci(wins, buyers),
            **_bucket_confidence("", buckets),
        }
        if median:
            confidence["median_purchase_minute"] = median[1]
        return ok(
            result,
            filters=filters,
            sample_size=buyers,
            definitions={
                "buyers": "Games (one player on one hero) in which the item was bought.",
                "player_games": "All hero games under the filters, summed over heroes.",
                "purchase_rate": "buyers / player_games.",
                "median_purchase_minute": (
                    "Median minute of the first purchase; interval is distribution-free."
                ),
                "buyer_win_rate": "Share of buyers' games that were won.",
                "win_rate_by_timing": TIMING_DEFINITION + " Buckets are purchase-time quartiles.",
                "top_heroes_by_purchase_rate": (
                    "Heroes with at least min_sample games, by the share of their games that "
                    "buy the item."
                ),
            },
            confidence=confidence,
            caveats=caveats,
            source=data.sources,
            period=data.period,
            data_freshness=fetched,
        )

    @mcp.tool
    async def analyze_item(
        item: ItemName,
        hero: HeroName = None,
        filters: FiltersArg = None,
        timing_buckets: TimingBuckets = None,
        top_heroes: TopHeroes = 10,
    ) -> ToolResult[dict[str, Any]]:
        """Whether an item's effectiveness depends on when it is bought, and where it works best.

        Win rate by purchase-minute bucket (e.g. BKB before 15, 15-20, 20-25, 25+) with
        Wilson intervals, for one hero or all heroes, plus the heroes whose buyers out-win
        their non-buyers the most. Read the selection-bias caveat before drawing conclusions.
        """
        filters = filters or Filters()
        resolved = _resolve_item(item)
        if isinstance(resolved, ToolResult):
            return resolved
        hero_record = None
        if hero is not None:
            hero_record = _resolve_hero(hero)
            if isinstance(hero_record, ToolResult):
                return hero_record
        hero_ids = [hero_record.id] if hero_record else analytics.all_hero_ids()
        try:
            data = await analytics.load_hero_items(hero_ids, filters, [resolved.id])
        except StratzError as exc:
            if hero_record is None:
                return _stratz_error(exc, filters)
            return await _analyze_item_opendota(resolved, hero_record, filters, str(exc))
        stats, fetched = data.stats, data.fetched

        rows = analytics.merge_rows(stats[h].rows(resolved.id) for h in hero_ids)
        buyers, wins = analytics.totals(rows)
        caveats = [analytics.SELECTION_BIAS_CAVEAT, *data.caveats]
        subject = f"{_item_label(resolved)}" + (
            f" on {hero_record.display_name}" if hero_record else ""
        )
        if buyers < filters.min_sample:
            return insufficient_data(
                f"Only {buyers} games bought {subject} under these filters.",
                _widen_hint(filters),
                filters=filters,
                sample_size=buyers,
                caveats=caveats,
                source=data.sources,
                period=data.period,
                data_freshness=fetched,
            )
        boundaries = timing_buckets or analytics.quartile_boundaries(rows)
        buckets = analytics.timing_buckets(rows, boundaries)
        small = [b.label for b in buckets if b.games < filters.min_sample]
        if small:
            caveats.append(
                f"Buckets {', '.join(small)} have fewer than {filters.min_sample} games; "
                "their win rates are noisy, see the intervals."
            )
        lifts = analytics.hero_lifts((stats[h] for h in hero_ids), resolved.id, filters.min_sample)
        median = analytics.median_minute(rows)
        result: dict[str, Any] = {
            "item": _item_label(resolved),
            "item_id": resolved.id,
            "hero": hero_record.display_name if hero_record else None,
            "buyers": buyers,
            "buyer_win_rate": round(wins / buyers, 4),
            "median_purchase_minute": median[0] if median else None,
            "timing_boundaries": sorted(set(boundaries)),
            "win_rate_by_timing": [b.model_dump() for b in buckets],
        }
        definitions = {
            "buyers": "Games in which the item was bought.",
            "buyer_win_rate": "Share of buyers' games that were won.",
            "median_purchase_minute": "Median minute of the first purchase.",
            "win_rate_by_timing": TIMING_DEFINITION,
            "lift": (
                "Buyers' win rate minus the same hero's win rate in games without the item. "
                "q_value is Benjamini-Hochberg adjusted over every hero tested; heroes need "
                "min_sample games on both sides."
            ),
        }
        if hero_record is not None:
            hero_stats = stats[hero_record.id]
            result["hero_games"] = hero_stats.matches
            result["hero_win_rate"] = (
                round(hero_stats.wins / hero_stats.matches, 4) if hero_stats.matches else None
            )
            result["buyers_vs_non_buyers"] = lifts[0].model_dump() if lifts else None
            definitions["hero_win_rate"] = "The hero's win rate in all its games."
        else:
            result["best_heroes"] = [lift.model_dump() for lift in lifts[:top_heroes]]
            definitions["best_heroes"] = "Heroes ranked by lift, largest first."
        confidence = {
            "buyer_win_rate": analytics.rate_ci(wins, buyers),
            **_bucket_confidence("", buckets),
        }
        if median:
            confidence["median_purchase_minute"] = median[1]
        return ok(
            result,
            filters=filters,
            sample_size=buyers,
            definitions=definitions,
            confidence=confidence,
            caveats=caveats,
            source=data.sources,
            period=data.period,
            data_freshness=fetched,
        )

    async def _analyze_item_opendota(
        item: Item, hero: Hero, filters: Filters, reason: str
    ) -> ToolResult[dict[str, Any]]:
        caveats = [
            analytics.SELECTION_BIAS_CAVEAT,
            f"STRATZ was unavailable ({reason}); fell back to OpenDota /scenarios/itemTimings, "
            "which ignores the rank, position, patch and game-mode filters, only covers items "
            "costing at least 1,400 gold and uses fixed timing thresholds.",
        ]
        try:
            rows, fetched = await analytics.fetch_item_timings(hero.id, item.name)
        except OpenDotaError as exc:
            return error(
                f"Neither STRATZ ({reason}) nor OpenDota ({exc}) could be reached.",
                hint=retry_hint(exc),
                filters=filters,
                source=["stratz", "opendota"],
            )
        buckets = analytics.opendota_timing_buckets(rows)
        games = sum(b.games for b in buckets)
        wins = sum(b.wins for b in buckets)
        if games < filters.min_sample:
            return insufficient_data(
                f"OpenDota has only {games} games of {hero.display_name} buying "
                f"{_item_label(item)}.",
                "Retry later when STRATZ is available, or lower min_sample.",
                filters=filters,
                sample_size=games,
                caveats=caveats,
                source=["opendota"],
                data_freshness=fetched,
            )
        return ok(
            {
                "item": _item_label(item),
                "item_id": item.id,
                "hero": hero.display_name,
                "buyers": games,
                "buyer_win_rate": round(wins / games, 4),
                "win_rate_by_timing": [b.model_dump() for b in buckets],
            },
            filters=filters,
            sample_size=games,
            definitions={
                "win_rate_by_timing": (
                    "Games in which the item was bought after the previous threshold and "
                    "before this one (OpenDota thresholds), with a 95% Wilson interval."
                ),
            },
            confidence=_bucket_confidence("", buckets),
            caveats=caveats,
            source=["opendota"],
            data_freshness=fetched,
        )

    @mcp.tool
    async def compare_items(
        items: Annotated[
            list[str],
            Field(
                min_length=2,
                max_length=4,
                description='2-4 items for the same slot, e.g. ["bkb", "linken"].',
            ),
        ],
        hero: Annotated[str, Field(description='Hero name or slang, e.g. "Storm Spirit".')],
        filters: FiltersArg = None,
        timing_buckets: TimingBuckets = None,
    ) -> ToolResult[dict[str, Any]]:
        """Compare alternative items on one hero within the same purchase-time buckets.

        An item bought earlier on average would win more just from timing, so win rates are
        compared bucket by bucket and as a timing-standardized total, with z-tests and
        Benjamini-Hochberg correction.
        """
        filters = filters or Filters()
        resolved_items: list[Item] = []
        for query in items:
            resolved = _resolve_item(query)
            if isinstance(resolved, ToolResult):
                return resolved
            resolved_items.append(resolved)
        if len({i.id for i in resolved_items}) != len(resolved_items):
            return error("The items list names the same item twice.", hint="Pass distinct items.")
        hero_record = _resolve_hero(hero)
        if isinstance(hero_record, ToolResult):
            return hero_record
        try:
            data = await analytics.load_hero_items(
                [hero_record.id], filters, [i.id for i in resolved_items]
            )
        except StratzError as exc:
            return _stratz_error(exc, filters)
        stats, fetched = data.stats, data.fetched

        hero_stats = stats[hero_record.id]
        item_rows = {_item_label(i): hero_stats.rows(i.id) for i in resolved_items}
        caveats = [
            analytics.SELECTION_BIAS_CAVEAT,
            "Players choose between these items depending on the game (enemy lineup, how "
            "the game is going), so differences are not a clean A/B test.",
            "One game can buy several of the compared items, so the groups can overlap and "
            "the z-tests, which assume independent groups, are approximate.",
            *data.caveats,
        ]
        counts = {name: analytics.totals(rows)[0] for name, rows in item_rows.items()}
        short = {name: n for name, n in counts.items() if n < filters.min_sample}
        if short:
            listed = ", ".join(f"{name} ({n})" for name, n in short.items())
            return insufficient_data(
                f"Too few {hero_record.display_name} games bought {listed}.",
                _widen_hint(filters),
                filters=filters,
                sample_size=min(counts.values()),
                caveats=caveats,
                source=data.sources,
                period=data.period,
                data_freshness=fetched,
            )
        boundaries = timing_buckets or analytics.quartile_boundaries(
            analytics.merge_rows(item_rows.values())
        )
        per_item, adjusted, tests = analytics.compare_buckets(item_rows, boundaries)
        summary = []
        for name, rows in item_rows.items():
            n, w = analytics.totals(rows)
            median = analytics.median_minute(rows)
            summary.append(
                {
                    "item": name,
                    "buyers": n,
                    "purchase_rate": round(n / hero_stats.matches, 4)
                    if hero_stats.matches
                    else None,
                    "raw_win_rate": round(w / n, 4),
                    "raw_win_rate_ci": analytics.rate_ci(w, n),
                    "median_purchase_minute": median[0] if median else None,
                    "win_rate_by_timing": [b.model_dump() for b in per_item[name]],
                }
            )
        confidence = {f"timing_adjusted_win_rate[{a.item}]": a.win_rate_ci for a in adjusted}
        for entry in summary:
            confidence[f"raw_win_rate[{entry['item']}]"] = entry["raw_win_rate_ci"]
        return ok(
            {
                "hero": hero_record.display_name,
                "hero_games": hero_stats.matches,
                "timing_boundaries": sorted(set(boundaries)),
                "items": summary,
                "timing_adjusted_win_rate": [a.model_dump() for a in adjusted],
                "tests": [t.model_dump() for t in tests],
            },
            filters=filters,
            sample_size=sum(counts.values()),
            definitions={
                "raw_win_rate": "Share of the item's buyers' games won, ignoring timing.",
                "win_rate_by_timing": TIMING_DEFINITION,
                "timing_adjusted_win_rate": (
                    "Win rate with every item weighted by the same purchase-time mix (the "
                    "share of all compared purchases per bucket, over buckets all items have "
                    "games in), so an item bought earlier gets no head start."
                ),
                "tests": (
                    "Two-proportion z-tests per bucket (both items need 30+ games) and on the "
                    "timing-adjusted rates; q_value is Benjamini-Hochberg adjusted over all of "
                    "them and significant means q_value <= 0.05."
                ),
            },
            confidence=confidence,
            caveats=caveats,
            source=data.sources,
            period=data.period,
            data_freshness=fetched,
        )

    @mcp.tool
    async def get_item_payback(
        items: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=4,
                description='1-4 farming items, e.g. ["battle fury", "midas", "radiance"].',
            ),
        ],
        hero: HeroName = None,
        filters: FiltersArg = None,
    ) -> ToolResult[dict[str, Any]]:
        """Extra gold per minute after buying a farming item, and how long it takes to pay back.

        Compares the buyer's gold income over the 10 minutes after the purchase with games of
        the same hero that had not bought it yet, net of any lead the buyer already had.
        Based on parsed league (pro) matches from OpenDota.
        """
        filters = filters or Filters()
        resolved_items: list[Item] = []
        for query in items:
            resolved = _resolve_item(query)
            if isinstance(resolved, ToolResult):
                return resolved
            resolved_items.append(resolved)
        hero_record = None
        if hero is not None:
            hero_record = _resolve_hero(hero)
            if isinstance(hero_record, ToolResult):
                return hero_record
        patch = analytics.explorer_patch(filters)
        caveats = [
            "Payback is estimated from parsed league (pro) matches on OpenDota, the only "
            "matches its database exposes with per-minute gold; pub games may differ.",
            "rank_bracket, position and game_mode filters do not apply to league matches.",
            f"Patch {patch} (OpenDota groups lettered patches under the major version).",
            "The estimate is correlational: games where the item fits (free farm, a good "
            "lane) also gain gold faster for other reasons. The before/after baseline removes "
            "a lead the buyer already had, not everything.",
        ]
        if hero_record is None:
            caveats.append(
                "Without a hero only buyers are fetched, so controls are buyers who got the "
                "item later; pass a hero for a better baseline."
            )
        estimates = []
        fetched = None
        try:
            for item in resolved_items:
                rows, when = await analytics.fetch_payback_rows(
                    item.name, hero_record.id if hero_record else None, patch
                )
                fetched = when if fetched is None else min(fetched, when)
                estimates.append((item, analytics.payback_estimate(rows, item.cost or 0)))
        except (OpenDotaError, RuntimeError) as exc:
            return error(
                f"OpenDota explorer query failed: {exc}",
                hint="Retry later, or pass a hero to make the query smaller.",
                filters=filters,
                source=["opendota"],
            )

        used = [e for _, e in estimates if e.buyers_used >= filters.min_sample]
        sample = min((e.buyers_used for _, e in estimates), default=0)
        if len(used) < len(estimates):
            missing = ", ".join(
                f"{_item_label(i)}: {e.buyers_used} of {e.buyers} buyers usable"
                for i, e in estimates
                if e.buyers_used < filters.min_sample
            )
            return insufficient_data(
                "Not enough league games with per-minute gold to estimate payback "
                f"({missing}). A buyer is usable when the game lasted 10+ minutes past the "
                f"purchase, it was bought after minute {analytics.PAYBACK_PRE}, and at least "
                f"{analytics.MIN_CONTROLS} games of the same hero had not bought it yet.",
                "Pass a hero that builds the item, use an older patch, or lower min_sample.",
                filters=filters,
                sample_size=sample,
                caveats=caveats,
                source=["opendota"],
                data_freshness=fetched,
            )
        results = []
        confidence: dict[str, tuple[float, float]] = {}
        for item, est in estimates:
            label = _item_label(item)
            results.append(
                {
                    "item": label,
                    "item_id": item.id,
                    "cost": item.cost,
                    "games": est.games,
                    "buyers": est.buyers,
                    "buyers_used": est.buyers_used,
                    "median_purchase_minute": est.median_purchase_minute,
                    "extra_gpm": est.extra_gpm,
                    "payback_minutes": est.payback_minutes,
                }
            )
            if est.extra_gpm_ci is not None:
                confidence[f"extra_gpm[{label}]"] = tuple(est.extra_gpm_ci)
            if est.payback_minutes_ci is not None:
                confidence[f"payback_minutes[{label}]"] = est.payback_minutes_ci
            if est.payback_minutes is None:
                caveats.append(
                    f"{label}: the extra-GPM interval is not above zero, so no payback "
                    "time is given."
                )
        return ok(
            {
                "hero": hero_record.display_name if hero_record else None,
                "patch": patch,
                "items": results,
            },
            filters=filters,
            sample_size=sample,
            definitions={
                "extra_gpm": (
                    "Mean over buyers of (gold per minute in the 10 minutes after the purchase "
                    "minus that of same-hero games that had not bought the item by then) minus "
                    "the same difference over the 5 minutes before; gold is total gold earned "
                    "(gold_t). Bootstrap 95% interval."
                ),
                "payback_minutes": (
                    "cost / extra_gpm: minutes of the extra income needed to earn back the "
                    "item's cost. Null when extra_gpm's interval includes zero."
                ),
                "buyers_used": "Buyers with a full window and enough comparison games.",
                "median_purchase_minute": "Median minute of the first purchase among buyers.",
            },
            confidence=confidence,
            caveats=caveats,
            source=["opendota"],
            data_freshness=fetched,
        )
