"""Farming tools: hero benchmarks, farm at fixed minutes, gold/XP curves, power curve, laning."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import farming
from dota_insights.mcp.envelope import ToolResult, error, insufficient_data, ok
from dota_insights.models import Filters
from dota_insights.reference import Hero, resolve_hero
from dota_insights.sources.opendota import OpenDotaError

HeroArg = Annotated[
    str,
    Field(description='Hero as a person would type it: "Anti-Mage", "am", "Фантомка", "pa".'),
]
FiltersArg = Annotated[
    Filters | None,
    Field(description="Which matches to use. Omit for all ranks and positions."),
]


def _hero(query: str) -> Hero | ToolResult:
    resolution = resolve_hero(query)
    if resolution.best is not None:
        return resolution.best
    if not resolution.found:
        return error(f"No hero matches {query!r}.", hint="Check the spelling of the hero name.")
    names = ", ".join(c.record.display_name for c in resolution.candidates[:5])
    return error(f"{query!r} could match several heroes: {names}.", hint="Name one of them.")


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_hero_benchmarks(
        hero: HeroArg,
        filters: FiltersArg = None,
        value: Annotated[
            dict[str, float] | None,
            Field(
                description=(
                    'Optional values to place on the distribution, e.g. {"gpm": 520, '
                    '"xpm": 610}. Keys: gpm, xpm, last_hits_per_min, hero_damage_per_min, '
                    "kills_per_min, deaths_per_min, assists_per_min, denies_per_min, "
                    "hero_healing_per_min, tower_damage."
                )
            ),
        ] = None,
    ) -> ToolResult[farming.HeroBenchmarks]:
        """Percentiles (p10..p99) of a hero's GPM, XPM, last hits/min, hero damage/min and
        more, and which percentile given values fall at. Use to answer "is 520 GPM good on
        this hero?"."""
        found = _hero(hero)
        if isinstance(found, ToolResult):
            return found
        filters = filters or Filters()
        try:
            bracket, role = farming.benchmark_params(filters)
        except ValueError as exc:
            return error(str(exc), filters=filters, source=["opendota"])
        unknown = [k for k in value or {} if farming.metric_name(k) is None]
        if unknown:
            return error(
                f"Unknown benchmark metric(s): {', '.join(unknown)}.",
                hint="Use one of: " + ", ".join(farming.BENCHMARK_METRICS),
            )
        try:
            payload = await farming.fetch_benchmarks(found.id, bracket, role)
        except OpenDotaError as exc:
            return error(f"OpenDota benchmarks failed: {exc}", source=["opendota"])
        metrics = farming.parse_benchmarks(payload)
        if not metrics:
            return insufficient_data(
                f"OpenDota has no benchmarks for {found.display_name} with these filters.",
                "Drop rank_bracket or position.",
                source=["opendota"],
            )
        placements = []
        for key, number in (value or {}).items():
            name = farming.metric_name(key)
            if name in metrics:
                pct, label = farming.place_value(metrics[name], number)
                placements.append(
                    farming.ValuePlacement(metric=name, value=number, percentile=pct, label=label)
                )
        medal = filters.rank_range[0].value if filters.rank_range else None
        caveats = [
            "OpenDota does not publish how many matches the percentiles rest on, so no "
            "sample size or confidence interval is given; the filters used are in the result.",
            "OpenDota computes benchmarks over recent matches across patches; the patch "
            "filter is not applied.",
        ]
        if role:
            caveats.append(
                f'OpenDota splits only by role ("{role}"), not by exact position: positions '
                "1-3 count as core and 4-5 as support."
            )
        return ok(
            farming.HeroBenchmarks(
                hero=found.display_name,
                hero_id=found.id,
                bracket=medal,
                role=role,
                metrics=metrics,
                placements=placements,
            ),
            definitions={
                **farming.BENCHMARK_METRICS,
                "percentile": "Share of the hero's matches with this value or lower.",
                "placements": "Linear interpolation between neighbouring published percentiles.",
            },
            caveats=caveats,
            source=["opendota"],
        )

    @mcp.tool
    async def get_hero_power_curve(
        hero: HeroArg, filters: FiltersArg = None
    ) -> ToolResult[farming.PowerCurve]:
        """Win rate by match duration (5-minute buckets), the duration where the hero wins
        most, and whether it is an early-game or late-game hero."""
        found = _hero(hero)
        if isinstance(found, ToolResult):
            return found
        requested = filters or Filters()
        used = Filters(min_sample=requested.min_sample)
        try:
            rows = await farming.fetch_durations(found.id)
        except OpenDotaError as exc:
            return error(f"OpenDota durations failed: {exc}", source=["opendota"])
        buckets = farming.bucket_durations(rows)
        n = sum(b.games for b in buckets)
        caveats = [
            "OpenDota's duration table covers the matches in its own match database, which "
            "is mostly league (pro and amateur-league) games, not ranked pubs.",
            "Win rate by duration is correlational: long games also mean the hero's team "
            "was not stomped early.",
        ]
        ignored = [
            name
            for name in ("patch", "rank_bracket", "position", "game_mode")
            if getattr(requested, name) is not None
        ]
        if ignored:
            caveats.append(
                f"This source cannot filter by {', '.join(ignored)}; the result covers all of them."
            )
        if n < used.min_sample:
            return insufficient_data(
                f"Only {n} matches with {found.display_name} in OpenDota's duration table.",
                "Lower min_sample to accept a noisier answer.",
                filters=used,
                sample_size=n,
                caveats=caveats,
                source=["opendota"],
            )
        curve = farming.power_curve(found.display_name, found.id, buckets)
        return ok(
            curve,
            filters=used,
            sample_size=n,
            definitions={
                "win_rate": "Wins / games in the bucket, by match duration.",
                "peak": (
                    "Bucket with the highest win rate among buckets with at least "
                    f"{farming.MIN_BUCKET_GAMES} games."
                ),
                "classification": (
                    f"Compares win rate in matches under {farming.EARLY_BEFORE_MIN} min "
                    f"(early) with matches of {farming.LATE_FROM_MIN}+ min (late) using a "
                    f"pooled two-proportion z-test; 'late' or 'early' if p < "
                    f"{farming.POWER_ALPHA}, else 'flat'."
                ),
            },
            confidence={f"win_rate[{b.label}]": (b.ci_low, b.ci_high) for b in curve.buckets},
            caveats=caveats,
            source=["opendota"],
        )
