"""Farming tools: hero benchmarks, farm at fixed minutes, gold/XP curves, power curve, laning."""

from datetime import timedelta
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import farming
from dota_insights.analytics.stats import wilson_interval
from dota_insights.db import readers
from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.mcp.errors import source_error
from dota_insights.models import Filters, Outcome
from dota_insights.reference import Hero, resolve_hero
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

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


OutcomeArg = Annotated[
    Outcome,
    Field(description='"win" or "loss" to use only matches the hero won or lost; "all" for both.'),
]

LEAGUE_CAVEAT = (
    "Computed from parsed league matches (pro and amateur leagues) in OpenDota's explorer, "
    "not ranked pubs: OpenDota only exposes per-minute arrays in bulk for league games, and "
    "only ~8% of pubs are parsed. Expect league farm to differ from pub farm."
)
GOLD_DEFINITION = (
    "OpenDota gold_t at the minute: the player's total gold as recorded by OpenDota's "
    "replay parser (close to, but not the same as, STRATZ net worth)."
)


def _explorer_filters(requested: Filters) -> tuple[Filters, list[str]] | ToolResult:
    """Filters the explorer can apply (patch, min_sample) and caveats for the dropped ones."""
    try:
        patch = farming.explorer_patch(requested)
    except ValueError as exc:
        return error(str(exc), filters=requested)
    caveats = [LEAGUE_CAVEAT]
    if requested.patch and requested.patch.strip().lower() != patch:
        caveats.append(f"Patch letters are not tracked; {requested.patch} was widened to {patch}.")
    ignored = [n for n in ("rank_bracket", "position", "game_mode") if getattr(requested, n)]
    if ignored:
        caveats.append(
            f"League matches cannot be filtered by {', '.join(ignored)}; the result covers "
            "all of them."
        )
    return Filters(patch=patch, min_sample=requested.min_sample), caveats


def _period(rows) -> Period | None:
    span = farming.match_period(rows)
    return Period(from_date=span[0], to_date=span[1]) if span else None


def _not_enough(name: str, n: int, used: Filters, caveats: list[str]) -> ToolResult:
    return insufficient_data(
        f"Only {n} parsed league matches with {name} on patch {used.patch}.",
        "Use an older patch, set outcome to all, or lower min_sample.",
        filters=used,
        sample_size=n,
        caveats=caveats,
        source=["opendota"],
    )


def _curve_definitions() -> dict[str, str]:
    return {
        "win_rate": "Wins / games in the bucket, by match duration.",
        "peak": (
            f"Bucket starting at {farming.PEAK_FROM_MIN}+ min with the highest win rate and at "
            f"least {farming.MIN_BUCKET_GAMES} games (shorter games are mostly abandons)."
        ),
        "classification": (
            f"Compares win rate in matches under {farming.EARLY_BEFORE_MIN} min (early) with "
            f"matches of {farming.LATE_FROM_MIN}+ min (late) using a pooled two-proportion "
            f"z-test; 'late' or 'early' if p < {farming.POWER_ALPHA}, else 'flat'."
        ),
    }


CORRELATION_CAVEAT = (
    "Win rate by duration is correlational: long games also mean the hero's team was not "
    "stomped early."
)


async def _pub_power_curve(found: Hero, requested: Filters) -> ToolResult:
    patch, start, end = farming.patch_window(requested)
    used = requested.model_copy(
        update={
            "patch": patch,
            "game_mode": requested.game_mode or farming.STRATZ_DEFAULT_MODE,
        }
    )
    data = await farming.fetch_stratz(
        farming.POWER_CURVE_DOC, farming.power_curve_variables(found.id, used)
    )
    rows, weeks = farming.stratz_duration_rows(data["heroStats"]["winWeek"] or [], start, end)
    buckets = farming.bucket_durations(rows)
    n = sum(b.games for b in buckets)
    caveats = [
        CORRELATION_CAVEAT,
        "Ranked pub data from STRATZ (stratz.com), summed over weekly buckets that start "
        f"inside patch {patch}. STRATZ keeps only recent weeks, so the period can be shorter "
        "than the patch.",
        "STRATZ's durationMinute field is read as a 5-minute bucket index; STRATZ does not "
        f"document it. Matches under {farming.STRATZ_MIN_DURATION_MIN} min are left out.",
    ]
    if requested.patch and requested.patch.strip().lower() != patch:
        caveats.append(f"Patch letters are not tracked; {requested.patch} was widened to {patch}.")
    period = None
    if weeks:
        period = Period(from_date=weeks[0], to_date=weeks[-1] + timedelta(days=6))
    if n < used.min_sample:
        return insufficient_data(
            f"Only {n} STRATZ matches with {found.display_name} for these filters.",
            "Widen rank_bracket, drop position, or use a newer patch.",
            filters=used,
            sample_size=n,
            period=period,
            caveats=caveats,
            source=["stratz"],
        )
    curve = farming.power_curve(found.display_name, found.id, buckets)
    return ok(
        curve,
        filters=used,
        sample_size=n,
        period=period,
        definitions=_curve_definitions(),
        confidence={f"win_rate[{b.label}]": (b.ci_low, b.ci_high) for b in curve.buckets},
        caveats=caveats,
        source=["stratz"],
    )


async def _league_power_curve(found: Hero, requested: Filters, fallback: str) -> ToolResult:
    used = Filters(min_sample=requested.min_sample)
    try:
        rows = await farming.fetch_durations(found.id)
    except OpenDotaError as exc:
        return source_error(exc, source=["opendota"])
    buckets = farming.bucket_durations(rows)
    n = sum(b.games for b in buckets)
    caveats = [
        fallback,
        "OpenDota's duration table covers the matches in its own match database, which is "
        "mostly league (pro and amateur-league) games, not ranked pubs.",
        CORRELATION_CAVEAT,
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
        definitions=_curve_definitions(),
        confidence={f"win_rate[{b.label}]": (b.ci_low, b.ci_high) for b in curve.buckets},
        caveats=caveats,
        source=["opendota"],
    )


async def _league_lane_stats(found: Hero, requested: Filters, fallback: str) -> ToolResult:
    prepared = _explorer_filters(requested)
    if isinstance(prepared, ToolResult):
        return prepared
    used, caveats = prepared
    try:
        rows = await farming.fetch_explorer(farming.lane_sql(found.id, used.patch))
    except OpenDotaError as exc:
        return source_error(exc, source=["opendota"])
    stats = farming.lane_stats(found.display_name, found.id, used.patch, rows)
    if stats.games < used.min_sample:
        return _not_enough(found.display_name, stats.games, used, caveats)
    wins = round(stats.lane_win_rate * stats.games)
    losses = round(stats.lane_loss_rate * stats.games)
    caveats += [
        fallback,
        "Lane outcome is our own rule on gold at 10 minutes (see definitions), not "
        "STRATZ's lane outcome.",
        f"Opponents and partners need at least {farming.MIN_PAIR_GAMES} shared lanes to "
        "be ranked; with this sample their rates are noisy.",
    ]
    return ok(
        stats,
        filters=used,
        sample_size=stats.games,
        period=_period(rows),
        definitions={
            "lane outcome": (
                "Per match, the average gold_t at minute 10 of the hero's side of its lane "
                "vs the enemies in that lane: edge = (own - enemy) / mean of both. Win if "
                f"edge >= {farming.LANE_MARGIN:.0%}, loss if <= -{farming.LANE_MARGIN:.0%}, "
                "else draw. Roaming heroes and empty lanes are skipped."
            ),
            "gold_10": "The hero's own " + GOLD_DEFINITION,
            "last_hits_10": "The hero's last hits at minute 10.",
            "match_win_rate_after_lane_win": "Match win rate in matches where it won lane.",
            "best_opponents": "Ranked by lane win rate minus lane loss rate.",
        },
        confidence={
            "lane_win_rate": tuple(wilson_interval(wins, stats.games)),
            "lane_loss_rate": tuple(wilson_interval(losses, stats.games)),
        },
        caveats=caveats,
        source=["opendota"],
        data_freshness=farming.newest_match(rows),
    )


def _pair_range(pairs: list[str] | None) -> str | None:
    """Filters.rank_bracket covering STRATZ medal pairs, e.g. ["DIVINE_IMMORTAL"] -> "divine+"."""
    if not pairs:
        return None
    low = pairs[0].split("_")[0].lower()
    high = pairs[-1].split("_")[1].lower()
    return f"{low}-{high}"


async def _pub_lane_stats(found: Hero, requested: Filters) -> ToolResult:
    pairs = farming.stratz_basic_brackets(requested)
    used = Filters(
        rank_bracket=_pair_range(pairs),
        position=requested.position,
        min_sample=requested.min_sample,
    )
    data = await farming.fetch_stratz(farming.LANE_DOC, farming.lane_variables(found.id, used))
    stats = farming.pub_lane_stats(found.display_name, found.id, used, data)
    caveats = [
        "Ranked pub data from STRATZ (stratz.com) over STRATZ's own recent window; STRATZ "
        "does not say which dates or patch it covers, so no period is given.",
        "Lane win/draw/loss and stomps are STRATZ's lane-outcome classification.",
        "Rates count hero-vs-lane-opponent pairings, so a match against two lane enemies "
        "counts twice; the confidence intervals treat pairings as independent and are "
        "slightly too narrow.",
        f"Opponents and partners need at least {farming.STRATZ_MIN_PAIR_GAMES} pairings to "
        "be ranked.",
    ]
    if requested.rank_bracket and used.rank_bracket != requested.rank_bracket:
        caveats.append(
            f"STRATZ groups medals in pairs for lane data; {requested.rank_bracket} was "
            f"widened to {used.rank_bracket}."
        )
    ignored = [n for n in ("patch", "game_mode") if getattr(requested, n)]
    if ignored:
        caveats.append(f"Lane data cannot be filtered by {', '.join(ignored)}.")
    if stats.pairings < used.min_sample:
        return insufficient_data(
            f"Only {stats.pairings} STRATZ lane pairings for {found.display_name}.",
            "Widen rank_bracket or drop position.",
            filters=used,
            sample_size=stats.pairings,
            caveats=caveats,
            source=["stratz"],
        )
    n = stats.pairings
    return ok(
        stats,
        filters=used,
        sample_size=n,
        definitions={
            "pairings": "Hero-vs-lane-opponent pairings (sample_size counts these).",
            "lane_win_rate": "Pairings STRATZ classifies as a won lane / all pairings.",
            "stomp_win_rate": "Pairings STRATZ classifies as a lane stomp in the hero's favour.",
            "networth_10_avg": "STRATZ mean net worth at minute 10 for these filters.",
            "last_hits_10_avg": "STRATZ mean last hits at minute 10 for these filters.",
            "best_opponents": "Lane enemies ranked by lane win rate minus lane loss rate.",
            "best_partners": "Lane allies ranked the same way.",
        },
        confidence={
            "lane_win_rate": tuple(wilson_interval(round(stats.lane_win_rate * n), n)),
            "lane_loss_rate": tuple(wilson_interval(round(stats.lane_loss_rate * n), n)),
        },
        caveats=caveats,
        source=["stratz"],
    )


def _placements(
    metrics: dict[str, list[farming.Percentile]], value: dict[str, float] | None
) -> list[farming.ValuePlacement]:
    placements = []
    for key, number in (value or {}).items():
        name = farming.metric_name(key)
        if name in metrics:
            pct, label = farming.place_value(metrics[name], number)
            placements.append(
                farming.ValuePlacement(metric=name, value=number, percentile=pct, label=label)
            )
    return placements


def _db_benchmarks(
    hero: Hero, filters: Filters, value: dict[str, float] | None, db: farming.DbBenchmarks
) -> ToolResult[farming.HeroBenchmarks]:
    position = filters.position
    role = None if position is None else ("core" if position <= 3 else "support")
    caveats = db.scope.caveats(detail=True)
    caveats.append(
        "The database keeps only GPM and XPM distributions; ask without a value (and with a "
        "single medal) for OpenDota's other benchmark metrics."
    )
    if position is not None:
        caveats.append(f"Percentiles are for position {int(position)} exactly, not the role.")
    return ok(
        farming.HeroBenchmarks(
            hero=hero.display_name,
            hero_id=hero.id,
            bracket=filters.rank_bracket,
            role=role,
            metrics=db.metrics,
            placements=_placements(db.metrics, value),
        ),
        filters=db.scope.filters(filters),
        sample_size=db.sample_size,
        period=db.scope.period,
        definitions={
            **{name: farming.BENCHMARK_METRICS[name] for name in farming.DB_BENCHMARKS},
            "percentile": (
                "Share of the hero's games with this value or lower, interpolated inside "
                "25-wide GPM/XPM histogram buckets."
            ),
            "placements": "Linear interpolation between neighbouring percentiles.",
            "sample_size": "Enriched games behind the smaller of the two distributions.",
        },
        caveats=caveats,
        source=readers.DETAIL_SOURCES,
        data_freshness=db.scope.freshness,
    )


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
        unknown = [k for k in value or {} if farming.metric_name(k) is None]
        if unknown:
            return error(
                f"Unknown benchmark metric(s): {', '.join(unknown)}.",
                hint="Use one of: " + ", ".join(farming.BENCHMARK_METRICS),
            )
        if farming.wants_db_benchmarks(filters, value):
            found_db = await farming.db_benchmarks(found.id, filters)
            if found_db is not None:
                return _db_benchmarks(found, filters, value, found_db)
        try:
            bracket, role = farming.benchmark_params(filters)
        except ValueError as exc:
            return error(str(exc), filters=filters, source=["opendota"])
        try:
            payload = await farming.fetch_benchmarks(found.id, bracket, role)
        except OpenDotaError as exc:
            return source_error(exc, source=["opendota"])
        metrics = farming.parse_benchmarks(payload)
        if not metrics:
            return insufficient_data(
                f"OpenDota has no benchmarks for {found.display_name} with these filters.",
                "Drop rank_bracket or position.",
                source=["opendota"],
            )
        placements = _placements(metrics, value)
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
        most, and whether it is an early-game or late-game hero. Uses ranked pubs from
        STRATZ by medal and position; falls back to OpenDota league matches."""
        found = _hero(hero)
        if isinstance(found, ToolResult):
            return found
        requested = filters or Filters()
        try:
            return await _pub_power_curve(found, requested)
        except StratzError as exc:
            fallback = f"STRATZ was unavailable ({exc}); this falls back to OpenDota."
        except ValueError as exc:
            return error(str(exc), filters=requested)
        return await _league_power_curve(found, requested, fallback)

    @mcp.tool
    async def get_farming_stats(
        hero: HeroArg,
        filters: FiltersArg = None,
        minutes: Annotated[
            list[int],
            Field(description="Game minutes to report, 1-90.", min_length=1, max_length=20),
        ] = [10, 15, 20, 25, 30],  # noqa: B006 - pydantic copies defaults
        outcome: OutcomeArg = Outcome.ALL,
    ) -> ToolResult[farming.FarmingStats]:
        """Gold, GPM, XP, XPM and last hits of a hero at given minutes (median and
        quartiles). Use for "how much farm should Anti-Mage have at 15 minutes?"."""
        found = _hero(hero)
        if isinstance(found, ToolResult):
            return found
        if any(not 1 <= m <= 90 for m in minutes):
            return error("minutes must be between 1 and 90.")
        prepared = _explorer_filters(filters or Filters())
        if isinstance(prepared, ToolResult):
            return prepared
        used, caveats = prepared
        try:
            rows = await farming.fetch_explorer(farming.farm_sql(found.id, used.patch))
        except OpenDotaError as exc:
            return source_error(exc, source=["opendota"])
        rows = farming.filter_outcome(rows, outcome)
        if len(rows) < used.min_sample:
            return _not_enough(found.display_name, len(rows), used, caveats)
        stats = farming.farm_at_minutes(rows, sorted(set(minutes)), used.min_sample)
        short = [m.minute for m in stats if m.gold is None]
        if short:
            caveats.append(
                f"Fewer than {used.min_sample} matches lasted to minute(s) "
                f"{', '.join(map(str, short))}; those values are null."
            )
        caveats.append(
            f"Uses the newest {farming.LEAGUE_MATCH_LIMIT} parsed league matches at most."
        )
        return ok(
            farming.FarmingStats(
                hero=found.display_name,
                hero_id=found.id,
                patch=used.patch,
                outcome=outcome.value,
                minutes=stats,
            ),
            filters=used,
            sample_size=len(rows),
            period=_period(rows),
            definitions={
                "gold": GOLD_DEFINITION,
                "gpm": "gold / minute at that minute.",
                "xp": "Total experience at the minute (OpenDota xp_t).",
                "xpm": "xp / minute at that minute.",
                "last_hits": "Last hits at the minute (OpenDota lh_t).",
                "games": "Matches that lasted at least that many minutes.",
                "median/q1/q3": "50th, 25th and 75th percentile over those matches.",
            },
            caveats=caveats,
            source=["opendota"],
            data_freshness=farming.newest_match(rows),
        )

    @mcp.tool
    async def get_gold_xp_curves(
        heroes: Annotated[
            list[str],
            Field(
                description="1-5 heroes to compare, as a person would type them.",
                min_length=1,
                max_length=5,
            ),
        ],
        filters: FiltersArg = None,
        outcome: OutcomeArg = Outcome.ALL,
    ) -> ToolResult[list[farming.HeroCurves]]:
        """Minute-by-minute median gold and XP of several heroes, in won and lost matches
        side by side. Use to compare how fast heroes come online."""  # fmt: skip
        resolved = []
        for name in heroes:
            found = _hero(name)
            if isinstance(found, ToolResult):
                return found
            resolved.append(found)
        prepared = _explorer_filters(filters or Filters())
        if isinstance(prepared, ToolResult):
            return prepared
        used, caveats = prepared
        curves, all_rows, short = [], [], []
        for found in resolved:
            try:
                rows = await farming.fetch_explorer(farming.farm_sql(found.id, used.patch))
            except OpenDotaError as exc:
                return source_error(exc, source=["opendota"])
            rows = farming.filter_outcome(rows, outcome)
            if len(rows) < used.min_sample:
                short.append(f"{found.display_name} ({len(rows)})")
                continue
            all_rows.extend(rows)
            wins = farming.filter_outcome(rows, Outcome.WIN)
            losses = farming.filter_outcome(rows, Outcome.LOSS)
            curves.append(
                farming.HeroCurves(
                    hero=found.display_name,
                    hero_id=found.id,
                    games=len(rows),
                    wins=farming.median_curve(wins) if outcome is not Outcome.LOSS else None,
                    losses=farming.median_curve(losses) if outcome is not Outcome.WIN else None,
                )
            )
        if not curves:
            return insufficient_data(
                f"Too few parsed league matches on patch {used.patch}: {', '.join(short)}.",
                "Use an older patch, set outcome to all, or lower min_sample.",
                filters=used,
                caveats=caveats,
                source=["opendota"],
            )
        if short:
            caveats.append(
                f"Left out for having fewer than {used.min_sample} matches: {', '.join(short)}."
            )
        caveats.append(
            f"Each curve stops at the last minute that at least {farming.MIN_CURVE_GAMES} "
            "matches reached, so late minutes describe only long games."
        )
        return ok(
            curves,
            filters=used,
            sample_size=min(c.games for c in curves),
            period=_period(all_rows),
            definitions={
                "gold_median": "Median " + GOLD_DEFINITION,
                "xp_median": "Median total experience at the minute (OpenDota xp_t).",
                "games": "Matches still running at that minute.",
                "sample_size": "Matches behind the smallest of the returned heroes.",
            },
            caveats=caveats,
            source=["opendota"],
            data_freshness=farming.newest_match(all_rows),
        )

    @mcp.tool
    async def get_hero_lane_stats(
        hero: HeroArg, filters: FiltersArg = None
    ) -> ToolResult[farming.PubLaneStats | farming.LaneStats]:
        """How a hero's laning phase goes: lane win/draw/loss rates, farm at 10 minutes,
        and the lane opponents and lane partners it does best and worst with. Uses ranked
        pubs from STRATZ by medal and position; falls back to OpenDota league matches."""
        found = _hero(hero)
        if isinstance(found, ToolResult):
            return found
        requested = filters or Filters()
        try:
            return await _pub_lane_stats(found, requested)
        except StratzError as exc:
            fallback = f"STRATZ was unavailable ({exc}); this falls back to OpenDota."
        return await _league_lane_stats(found, requested, fallback)
