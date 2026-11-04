"""Hero meta tools: overview, comparison, tier list and hidden gems."""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import heroes as analytics
from dota_insights.mcp.envelope import ToolResult, error, insufficient_data, ok
from dota_insights.models import Filters
from dota_insights.reference import Hero, resolve_hero
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

Metric = Literal["win_rate", "pick_rate", "kda", "xpm", "networth", "networth_per_min", "last_hits"]

FILTERS_DESCRIPTION = (
    "Which matches to use. Omitted patch means the live (lettered) patch; omitted game_mode "
    "means ranked_all_pick; rank_bracket accepts single medals including immortal."
)

ALL_MATCHES = Filters()

RATE_DEFINITIONS = {
    "win_rate": "wins / games for the hero under the filters.",
    "pick_rate": (
        "games with the hero / matches in the pool. With a position filter the pool is "
        "player-slots at that position / 2, so it is the share of matches where the hero "
        "played that position."
    ),
    "win_rate_ci": "95% Wilson score interval of the win rate.",
    "pick_rate_ci": "95% Wilson score interval of the pick rate.",
}
SMOOTHING_DEFINITION = (
    f"Empirical-Bayes posterior mean (wins + {analytics.PRIOR_STRENGTH} * p) / (games + "
    f"{analytics.PRIOR_STRENGTH}), where p is the pool win rate: every hero gets "
    f"{analytics.PRIOR_STRENGTH} pseudo-games at the average, so small samples are pulled "
    "towards it."
)


def _resolve(query: str) -> Hero | str:
    """The hero, or an error message listing candidates."""
    resolution = resolve_hero(query)
    if resolution.best is not None:
        return resolution.best
    if not resolution.found:
        return f"No hero matches {query!r}."
    names = ", ".join(c.record.display_name for c in resolution.candidates)
    return f"{query!r} is ambiguous: {names}. Ask which one is meant."


def _source_error(exc: Exception, filters: Filters) -> ToolResult:
    return error(
        f"Hero data source failed: {exc}",
        hint="Retry in a minute; if it persists, the upstream API may be down.",
        filters=filters,
    )


def _wrap(analysis: analytics.Analysis, filters: Filters, definitions, confidence) -> ToolResult:
    pool = analysis.pool
    if analysis.sample_size < filters.min_sample:
        return insufficient_data(
            f"Only {analysis.sample_size} games match these filters (min_sample "
            f"{filters.min_sample}).",
            "Widen rank_bracket (e.g. 'divine+' instead of 'immortal'), drop position, or use "
            "the whole patch (e.g. '7.41' instead of '7.41f').",
            filters=pool.filters,
            sample_size=analysis.sample_size,
            period=pool.period,
            caveats=analysis.caveats,
            source=analysis.sources,
            data_freshness=pool.freshness,
        )
    return ok(
        analysis.result,
        filters=pool.filters,
        sample_size=analysis.sample_size,
        period=pool.period,
        definitions=definitions,
        confidence=confidence,
        caveats=analysis.caveats,
        source=analysis.sources,
        data_freshness=pool.freshness,
    )


async def _run(coro, filters: Filters):
    try:
        return await coro
    except analytics.HeroDataUnavailable as exc:
        return error(str(exc), hint=exc.hint, filters=filters)
    except (StratzError, OpenDotaError) as exc:
        return _source_error(exc, filters)


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_hero_overview(
        hero: Annotated[
            str, Field(description='Hero name as a person would type it ("PA", "Invoker").')
        ],
        filters: Annotated[Filters, Field(description=FILTERS_DESCRIPTION)] = ALL_MATCHES,
    ) -> ToolResult[analytics.HeroOverview]:
        """Win and pick rate of one hero with 95% intervals, its win-rate rank, which
        positions it is played in, win rate per medal (Herald to Immortal), the trend over the
        last three patches, and pro pick/ban rates."""
        found = _resolve(hero)
        if isinstance(found, str):
            return error(found, hint="Pass one of the listed names.", filters=filters)
        analysis = await _run(analytics.hero_overview(found, filters), filters)
        if not isinstance(analysis, analytics.Analysis):
            return analysis
        current = analysis.result.current
        confidence = {"win_rate": current.win_rate_ci}
        if current.pick_rate_ci is not None:
            confidence["pick_rate"] = current.pick_rate_ci
        definitions = RATE_DEFINITIONS | {
            "win_rate_rank": "Position by raw win rate among heroes with >= min_sample games.",
            "by_position": (
                "Hero games per position (STRATZ position detection) over the same window, "
                "with win rate and share of the hero's games; rank filter applied."
            ),
            "by_bracket": (
                "Hero win rate per single medal (all eight, regardless of rank_bracket) "
                "over the same window; position filter applied."
            ),
            "typical_positions": "Positions with >= 20% of the hero's games.",
            "trend": "Win rate per lettered patch from weekly buckets; filters applied.",
            "pro.ban_rate": "Pro matches where the hero was banned / pro matches (OpenDota).",
        }
        return _wrap(analysis, filters, definitions, confidence)

    @mcp.tool
    async def compare_heroes(
        heroes: Annotated[
            list[str],
            Field(min_length=2, max_length=5, description="2 to 5 hero names to compare."),
        ],
        metrics: Annotated[
            list[Metric] | None,
            Field(description="Metrics to show. Omit for all of them."),
        ] = None,
        filters: Annotated[Filters, Field(description=FILTERS_DESCRIPTION)] = ALL_MATCHES,
    ) -> ToolResult[analytics.ComparisonResult]:
        """Side-by-side win rate, pick rate, KDA, XPM, net worth and last hits for 2-5 heroes,
        with deltas against the first hero and pairwise win-rate significance tests. Treat a
        pair with significant=false as no real difference."""
        resolved: list[Hero] = []
        for name in heroes:
            found = _resolve(name)
            if isinstance(found, str):
                return error(found, hint="Pass one of the listed names.", filters=filters)
            if any(h.id == found.id for h in resolved):
                return error(f"{found.display_name} is listed twice.", filters=filters)
            resolved.append(found)
        chosen = list(dict.fromkeys(metrics)) if metrics else list(analytics.METRICS)
        analysis = await _run(analytics.compare(resolved, filters, chosen), filters)
        if not isinstance(analysis, analytics.Analysis):
            return analysis
        confidence = {}
        for row in analysis.result.heroes:
            confidence[f"{row.hero}.win_rate"] = row.win_rate_ci
            if row.pick_rate_ci is not None:
                confidence[f"{row.hero}.pick_rate"] = row.pick_rate_ci
        definitions = RATE_DEFINITIONS | {
            "kda": "(average kills + average assists) / max(average deaths, 1), STRATZ.",
            "xpm": "average experience at game end / average game length in minutes, STRATZ.",
            "networth": "average net worth at game end, STRATZ.",
            "networth_per_min": "average end net worth / average game length in minutes.",
            "last_hits": "average last hits at game end, STRATZ.",
            "delta_vs_first": "value minus the first hero's value.",
            "win_rate_tests": (
                "Pooled two-proportion z-test of win rates for every pair, p-values "
                "Benjamini-Hochberg adjusted (q); significant when q <= 0.05."
            ),
            "sample_size": "Games of the hero with the fewest games.",
        }
        analysis.caveats.append(
            "GPM is not published by STRATZ's aggregate (the field is empty); net worth per "
            "minute is the closest available measure."
        )
        return _wrap(analysis, filters, definitions, confidence)

    @mcp.tool
    async def get_tier_list(
        filters: Annotated[Filters, Field(description=FILTERS_DESCRIPTION)] = ALL_MATCHES,
        limit: Annotated[
            int, Field(ge=1, le=130, description="How many heroes to return, best first.")
        ] = 40,
    ) -> ToolResult[analytics.TierList]:
        """Heroes ranked by sample-size-adjusted win rate and sorted into S, A, B, C and D
        tiers. Filter by position to get a tier list for one role."""
        analysis = await _run(analytics.tier_list(filters, limit), filters)
        if not isinstance(analysis, analytics.Analysis):
            return analysis
        cut = analytics.TIER_CUTOFFS
        tiers = ", ".join(f"{t} >= {c * 100:+.1f}" for t, c in cut)
        definitions = RATE_DEFINITIONS | {
            "smoothed_win_rate": SMOOTHING_DEFINITION + " Heroes are ranked by it.",
            "tier": (
                "From smoothed_win_rate minus pool_win_rate, in win-rate points: "
                f"{tiers}, D otherwise."
            ),
            "pool_win_rate": "Total wins / total games over all heroes in the pool.",
            "sample_size": "Matches in the pool.",
        }
        confidence = {f"{e.hero}.win_rate": e.win_rate_ci for e in analysis.result.heroes}
        analysis.caveats.append(
            "Win rate measures results, not hero strength in isolation: heroes picked mostly "
            "by specialists or as counters can look stronger than they are for everyone."
        )
        return _wrap(analysis, filters, definitions, confidence)

    @mcp.tool
    async def find_hidden_gems(
        filters: Annotated[Filters, Field(description=FILTERS_DESCRIPTION)] = ALL_MATCHES,
        max_pick_rate: Annotated[
            float,
            Field(gt=0, le=1, description="Only heroes picked in at most this share of matches."),
        ] = 0.05,
        limit: Annotated[int, Field(ge=1, le=50, description="How many heroes to return.")] = 10,
    ) -> ToolResult[analytics.HiddenGems]:
        """Rarely picked heroes that win more than average, after shrinking small samples
        towards the average. Check `significant` before calling a hero underrated."""
        analysis = await _run(analytics.hidden_gems(filters, max_pick_rate, limit), filters)
        if not isinstance(analysis, analytics.Analysis):
            return analysis
        definitions = RATE_DEFINITIONS | {
            "smoothed_win_rate": SMOOTHING_DEFINITION,
            "edge": (
                "smoothed_win_rate - pool_win_rate; a gem needs edge >= "
                f"{analytics.GEM_MIN_EDGE * 100:.1f} points, pick_rate <= max_pick_rate and "
                ">= min_sample games."
            ),
            "p_value": "Two-proportion z-test of the hero's win rate against all other games.",
            "q_value": "Benjamini-Hochberg adjusted p-value over all candidates checked.",
            "significant": "q_value <= 0.05.",
            "sample_size": "Matches in the pool.",
        }
        confidence = {f"{g.hero}.win_rate": g.win_rate_ci for g in analysis.result.gems}
        analysis.caveats.append(
            "Low-pick heroes are often played by specialists, which inflates their win rate."
        )
        return _wrap(analysis, filters, definitions, confidence)
