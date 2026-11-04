"""General game statistics: side win rates and durations, records, rank distribution."""

from datetime import datetime
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import general
from dota_insights.analytics.stats import wilson_interval
from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.models import Filters
from dota_insights.reference import patches, resolve_hero
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

GENERAL_DEFINITIONS = {
    "radiant_win_rate": "Radiant wins / matches with the filters, over the window.",
    "dire_win_rate": "1 - radiant_win_rate.",
    "duration": (
        "Match length from the horn to the ancient's fall (OpenDota duration), in minutes; "
        "p25/median/p75 are percentiles over the filtered matches."
    ),
    "regions": "Filtered matches per server region (cluster mapped to region) / matches.",
    "game_modes": "Public matches per game mode / all public matches in the window.",
}

RECORD_DEFINITIONS = {
    "value": (
        "The single-match value of the metric: a player's total for kills, deaths, assists, "
        "last_hits, denies, hero_damage, tower_damage, hero_healing; gold_per_min and "
        "xp_per_min are per minute; duration is the match length in seconds."
    ),
    "rank": "Position in OpenDota's top-100 list for that metric.",
}

RANK_DEFINITIONS = {
    "opendota_share": "Profiles at the medal / all ranked profiles OpenDota tracks.",
    "opendota_cumulative": "Share of ranked profiles at or below the medal.",
    "stratz_share": "Accounts at the medal / ranked accounts STRATZ tracks (rank 0 excluded).",
    "leaderboards": "Players on Valve's current-season Immortal leaderboard per division.",
}

STRATZ_CAVEAT = "Data from STRATZ (https://stratz.com)."


def _current_patch() -> str:
    return patches()[-1].name


def _period(start: datetime, end: datetime) -> Period:
    return Period(from_date=start.date(), to_date=end.date())


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_general_stats(
        filters: Annotated[
            Filters | None,
            Field(
                description=(
                    "Which matches to describe. game_mode defaults to ranked_all_pick; "
                    "rank_bracket uses the match's average medal. position is not "
                    "supported (these are match-level figures)."
                )
            ),
        ] = None,
    ) -> ToolResult[general.GeneralStats]:
        """Game-wide numbers for recent public matches: Radiant vs Dire win rate, match
        duration distribution, region shares and game-mode shares, with sample sizes.

        Use for questions like "does Radiant win more?", "how long are games now?" or
        "which modes are played most?". The window is the latest ~11 hours of public
        matches, so it always reflects the current patch.
        """
        filters = filters or Filters()
        if filters.position is not None:
            return error(
                "get_general_stats describes whole matches and cannot filter by position.",
                hint="Drop filters.position.",
                filters=filters,
            )
        current = _current_patch()
        if filters.patch and not filters.patch.startswith(current):
            return error(
                f"Only the current patch ({current}) is available: the tool reads the latest "
                "public matches.",
                hint="Drop filters.patch.",
                filters=filters,
            )
        filters = filters.model_copy(
            update={"game_mode": filters.game_mode or general.DEFAULT_GAME_MODE}
        )
        try:
            stats, used_stratz = await general.general_stats(filters)
        except general.UnsupportedFilter as exc:
            return error(str(exc), hint="Use rank_bracket 'divine+'.", filters=filters)
        except (OpenDotaError, RuntimeError) as exc:
            return error(f"OpenDota request failed: {exc}", source=["opendota"])

        caveats = [
            "Covers OpenDota's sample of public matches from the latest ~11 hours (window "
            "times in the result), so it leans towards the regions in prime time then.",
            "Matches with duration 0 (not started or abandoned at once) are excluded.",
        ]
        source: list = ["opendota", "stratz"] if used_stratz else ["opendota"]
        if used_stratz:
            caveats.append(f"Server regions mapped with STRATZ constants. {STRATZ_CAVEAT}")
        if stats is not None:
            unknown = sum(r.share for r in stats.regions if r.name == general.UNKNOWN_REGION)
            if unknown:
                caveats.append(
                    f"{unknown:.1%} of matches were played on server clusters neither source "
                    "maps to a region; they are listed as 'unknown'."
                )
        if filters.rank_bracket:
            caveats.append(
                "Rank uses the match's average medal; Immortal is counted inside Divine."
            )
        if stats is None or stats.matches < filters.min_sample:
            return insufficient_data(
                f"{stats.matches if stats else 0} matches match the filters in the window.",
                "Widen rank_bracket or choose a more popular game_mode.",
                filters=filters,
                sample_size=stats.matches if stats else 0,
                caveats=caveats,
                source=source,
            )
        return ok(
            stats,
            filters=filters.model_copy(update={"patch": filters.patch or current}),
            sample_size=stats.matches,
            period=_period(stats.window_start, stats.window_end),
            definitions=GENERAL_DEFINITIONS,
            confidence=general.side_confidence(stats),
            caveats=caveats,
            source=source,
            data_freshness=stats.window_end,
        )

    @mcp.tool
    async def get_records(
        metric: Annotated[
            str | None,
            Field(
                description=(
                    "Which record: kills, deaths, assists, gold_per_min (gpm), xp_per_min "
                    "(xpm), last_hits, denies, hero_damage, tower_damage, hero_healing, "
                    "duration (longest match). Omit to get the top record of every metric."
                )
            ),
        ] = None,
        hero: Annotated[
            str | None,
            Field(
                description=(
                    'Only records set on this hero, any spelling ("PA", "Фантомка"). '
                    "Not available for duration."
                )
            ),
        ] = None,
        limit: Annotated[
            int | None,
            Field(
                ge=1,
                le=100,
                description="Rows per metric. Defaults to 10 for one metric, 1 for all.",
            ),
        ] = None,
    ) -> ToolResult[list[general.MetricRecords]]:
        """Single-match records (most kills, highest GPM, longest match, ...) from
        OpenDota's top-100 lists, optionally only those set on one hero."""
        if metric is None:
            metrics = list(general.RecordMetric)
        else:
            parsed = general.parse_metric(metric)
            if parsed is None:
                names = ", ".join(m.value for m in general.RecordMetric)
                return error(f"Unknown record metric {metric!r}.", hint=f"Use one of: {names}.")
            metrics = [parsed]
        hero_id = None
        if hero is not None:
            resolution = resolve_hero(hero)
            if resolution.best is None:
                names = ", ".join(c.record.display_name for c in resolution.candidates)
                return error(
                    f"Hero {hero!r} is {'ambiguous' if names else 'unknown'}.",
                    hint=f"Did you mean: {names}?" if names else "Check the hero name.",
                )
            hero_id = resolution.best.id
            metrics = [m for m in metrics if m is not general.RecordMetric.DURATION]
            if not metrics:
                return error("Duration is a match-level record and has no hero.", hint="Drop hero.")
        try:
            result, raw = await general.records(metrics, hero_id, limit or (10 if metric else 1))
        except OpenDotaError as exc:
            return error(f"OpenDota request failed: {exc}", source=["opendota"])

        caveats = [
            "OpenDota keeps these top-100 lists over a recent rolling window, not all time; "
            "the period shows the dates the lists span.",
            "OpenDota does not say which game modes and lobbies feed the lists; outliers "
            "may come from Turbo or unusual lobbies. Check a match with its match_id.",
        ]
        if hero_id is not None:
            caveats.append(
                "Hero rows are only those inside the overall top 100; a hero's own best "
                "game may be missing if it did not reach that list."
            )
        period = freshness = None
        if raw:
            times = [general.timestamp(r["start_time"]) for r in raw]
            period, freshness = _period(min(times), max(times)), max(times)
        if not any(r.rows for r in result):
            subject = f"{resolution.best.display_name} game" if hero_id is not None else "row"
            return insufficient_data(
                f"No {subject} is in OpenDota's top-100 lists.",
                "Drop hero to see the overall records." if hero_id else "Try again later.",
                period=period,
                caveats=caveats,
                source=["opendota"],
            )
        return ok(
            result,
            period=period,
            definitions=RECORD_DEFINITIONS,
            caveats=caveats,
            source=["opendota"],
            data_freshness=freshness,
        )

    @mcp.tool
    async def get_rank_distribution() -> ToolResult[general.RankDistribution]:
        """Share of players at each rank medal (Herald to Immortal) and star, with the
        percentile at the top of each medal, from OpenDota and STRATZ, plus the size of
        the current-season Immortal leaderboard per region (STRATZ)."""
        try:
            opendota, fetched_at = await general.fetch_distributions()
        except OpenDotaError as exc:
            return error(f"OpenDota request failed: {exc}", source=["opendota"])
        caveats = [
            "Counts are player profiles each source tracks (players who used the site or "
            "appeared in a fetched match), not Valve's full player base; inactive and "
            "smurf accounts are included.",
            "Neither source dates its counts; data_freshness is when they were fetched.",
        ]
        source: list = ["opendota"]
        try:
            stratz = await general.fetch_stratz_ranks()
        except StratzError as exc:
            stratz = None
            caveats.append(f"STRATZ was unavailable ({exc}); only OpenDota counts are shown.")
        else:
            source.append("stratz")
            caveats.append(STRATZ_CAVEAT)
        dist = general.rank_distribution(opendota, stratz)
        confidence = {
            f"{row.medal.value}_opendota_share": tuple(
                wilson_interval(row.opendota_players, dist.opendota_total)
            )
            for row in dist.medals
        }
        return ok(
            dist,
            sample_size=dist.opendota_total,
            definitions=RANK_DEFINITIONS,
            confidence=confidence,
            caveats=caveats,
            source=source,
            data_freshness=fetched_at,
        )
