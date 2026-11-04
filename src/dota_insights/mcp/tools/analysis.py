"""Analysis tools: hypothesis tests, metric-win correlation, match search and a safe structured
query builder over the OpenDota explorer."""

from statistics import median
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import analysis as an
from dota_insights.analytics.farming import explorer_patch
from dota_insights.mcp import args
from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.models import Filters, GameMode, Outcome
from dota_insights.reference import Hero, hero_by_id, patches, resolve_hero, resolve_item
from dota_insights.sources.opendota import OpenDotaError

FiltersArg = Annotated[
    Filters | None,
    Field(description="Which matches to use. Omit for the current patch."),
]

LEAGUE_CAVEAT = (
    "Computed from parsed league matches (pro and amateur leagues) in OpenDota's explorer, "
    "not ranked pubs: OpenDota exposes per-player stats and purchase logs in bulk only for "
    "league games. League play differs from pubs, so check before generalising to pubs."
)
CAUSALITY_CAVEAT = (
    "This is observational data, not an experiment: groups differ in more than the condition "
    "(a hero that buys BKB early is often already ahead). A significant difference shows an "
    "association, not that the condition causes the outcome."
)
SELECTION_CAVEAT = (
    "Selection bias: metrics like GPM, net worth or kills are partly a result of winning "
    "(the winning team farms and fights more), and support metrics like wards depend on the "
    "role and the game's pace. Controlling for duration removes only part of that."
)

ITEM_CAVEAT = (
    "Item timing is confounded by game state and length: a hero that is ahead buys items "
    "sooner, and a short game leaves no time to buy a late item at all."
)


def _hero(query: str) -> Hero | ToolResult:
    resolution = resolve_hero(query)
    if resolution.best is not None:
        return resolution.best
    if not resolution.found:
        return error(f"No hero matches {query!r}.", hint="Check the spelling of the hero name.")
    names = ", ".join(c.record.display_name for c in resolution.candidates[:5])
    return error(f"{query!r} could match several heroes: {names}.", hint="Name one of them.")


def _item_key(query: str) -> str | ToolResult:
    resolution = resolve_item(query)
    if resolution.best is not None:
        return resolution.best.name
    if not resolution.found:
        return error(f"No item matches {query!r}.", hint="Check the spelling of the item name.")
    names = ", ".join(c.record.display_name or c.record.name for c in resolution.candidates[:5])
    return error(f"{query!r} could match several items: {names}.", hint="Name one of them.")


def _league_filters(requested: Filters) -> tuple[Filters, list[str]] | ToolResult:
    """Filters the league explorer tables can apply (patch, min_sample) plus caveats."""
    try:
        patch = explorer_patch(requested)
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
    span = an.match_period(rows)
    return Period(from_date=span[0], to_date=span[1]) if span else None


async def _rows(sql: str) -> list[dict[str, Any]] | ToolResult:
    try:
        return await an.fetch_explorer(sql)
    except OpenDotaError as exc:
        return error(
            f"OpenDota explorer failed: {exc}", hint=an.EXPLORER_TIMEOUT_HINT, source=["opendota"]
        )


def _conditions(
    conditions: list[an.Condition],
) -> tuple[list[an.ResolvedCondition], set[str]] | ToolResult:
    resolved, items = [], set()
    for cond in conditions:
        key = None
        if cond.item is not None:
            key = _item_key(cond.item)
            if isinstance(key, ToolResult):
                return key
            items.add(key)
        try:
            resolved.append(an.resolve_condition(cond, key))
        except ValueError as exc:
            return error(str(exc), hint="See the condition field descriptions.")
    return resolved, items


def _label(conditions: list[an.ResolvedCondition]) -> str:
    return " and ".join(c.label() for c in conditions) or "all matches"


_FIELD_LIST = "; ".join(
    f"{name} ({table.description} Fields: {', '.join(table.fields)})"
    for name, table in an.EXPLORE_TABLES.items()
)


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def test_hypothesis(
        hero: Annotated[str, Field(description=f"Hero whose matches are compared. {args.HERO}")],
        group_a: Annotated[
            list[an.Condition],
            Field(
                min_length=1,
                description=(
                    "Conditions that define group A (all must hold). Example for 'BKB before "
                    '20 min\': [{"field": "item_minute", "item": "bkb", "op": "<", '
                    '"value": 20}].'
                ),
            ),
        ],
        group_b: Annotated[
            list[an.Condition] | None,
            Field(
                description=(
                    "Conditions for group B. Omit to compare A with all the hero's other "
                    "matches. Example for 'BKB at 20 min or later': [{\"field\": "
                    '"item_minute", "item": "bkb", "op": ">=", "value": 20}].'
                )
            ),
        ] = None,
        outcome: Annotated[
            str,
            Field(
                description=(
                    '"win" to compare win rates, or a per-player metric to compare its '
                    "distribution (gold_per_min, xp_per_min, kills, deaths, assists, "
                    "last_hits, hero_damage, net_worth, obs_placed, camps_stacked, "
                    "duration_min, ...)."
                )
            ),
        ] = "win",
        filters: FiltersArg = None,
    ) -> ToolResult[an.HypothesisResult]:
        """Compare two groups of a hero's matches defined by structured conditions (item
        timing, side, lane, GPM, duration...) and test whether their win rate or a metric
        differs. Picks the test (chi-square / Fisher exact for win rate, Mann-Whitney U for
        a metric) and reports effect size, 95% CI, p-value and causality caveats. Use for
        "does PA win more with BKB before 20 minutes than after?"."""
        found = _hero(hero)
        if isinstance(found, ToolResult):
            return found
        target = outcome.strip().lower()
        if target != "win":
            target = an.metric_name(target) or ""
            if not target:
                return error(
                    f"Unknown outcome {outcome!r}.",
                    hint="Use 'win' or one of: " + ", ".join(an.PLAYER_METRICS),
                )
        parsed_a = _conditions(group_a)
        if isinstance(parsed_a, ToolResult):
            return parsed_a
        parsed_b = _conditions(group_b) if group_b is not None else ([], set())
        if isinstance(parsed_b, ToolResult):
            return parsed_b
        league = _league_filters(filters or Filters())
        if isinstance(league, ToolResult):
            return league
        used, caveats = league
        rows = await _rows(
            an.player_rows_sql(found.id, used.patch, sorted(parsed_a[1] | parsed_b[1]))
        )
        if isinstance(rows, ToolResult):
            return rows
        a, b, overlap = an.split_groups(rows, parsed_a[0], parsed_b[0] if group_b else None)
        n = len(a) + len(b)
        caveats = [CAUSALITY_CAVEAT, *caveats]
        if parsed_a[1] | parsed_b[1]:
            caveats.append(ITEM_CAVEAT)
        if overlap:
            caveats.append(f"{overlap} matches met both groups' conditions and were left out.")
        if n < used.min_sample or min(len(a), len(b)) < an.MIN_GROUP:
            return insufficient_data(
                f"Group A has {len(a)} and group B {len(b)} parsed league matches of "
                f"{found.display_name} on patch {used.patch} (need {used.min_sample} in total "
                f"and {an.MIN_GROUP} per group).",
                "Loosen the conditions, use an older patch, or lower min_sample.",
                filters=used,
                sample_size=n,
                caveats=caveats,
                source=["opendota"],
            )
        try:
            test = an.compare_groups(a, b, target)
        except ValueError as exc:
            return error(str(exc), filters=used, source=["opendota"])
        label_a = _label(parsed_a[0])
        label_b = _label(parsed_b[0]) if group_b else f"not ({label_a})"
        sum_a = an.summarize_group(a, label_a, target)
        sum_b = an.summarize_group(b, label_b, target)
        result = an.HypothesisResult(
            hero=found.display_name,
            patch=used.patch,
            outcome=target,
            group_a=sum_a,
            group_b=sum_b,
            significant=test["p_value"] < an.ALPHA,
            overlap_dropped=overlap,
            interpretation=an.interpret(
                target, test["effect_size"], test["p_value"], test["effect_ci"]
            ),
            **test,
        )
        if test["test"] == "fisher_exact":
            caveats.append("A group is small, so the exact Fisher test was used (statistic 0).")
        definitions = {
            "chi_square_2x2": (
                "Pearson chi-square test of the 2x2 table group x win (equal to the pooled "
                "two-proportion z-test squared), used when every expected cell is >= 5."
            ),
            "fisher_exact": "Two-sided Fisher exact test of the 2x2 table (small samples).",
            "mann_whitney_u": (
                "Two-sided Mann-Whitney U test (normal approximation with tie and continuity "
                "correction): does one group's metric tend to be higher? No normality assumed."
            ),
            "effect_size": (
                "Win rate A - win rate B (Newcombe hybrid score 95% CI), or median A - median "
                "B for a metric (percentile bootstrap 95% CI, 2,000 resamples, seed 0)."
            ),
            "standardized_effect": (
                "Cohen's h = 2·asin√p₁ - 2·asin√p₂ (0.2 small, 0.5 medium) for win rate; "
                "rank-biserial r = 2U/(n₁n₂) - 1 for a metric."
            ),
            "item_minute": "Game minute of the first purchase of the item, from purchase_log.",
            "win_rate_ci": "Wilson 95% interval of each group's win rate.",
        }
        if target != "win":
            definitions[target] = an.PLAYER_METRICS[target][1]
        return ok(
            result,
            filters=used,
            sample_size=n,
            period=_period(a + b),
            definitions=definitions,
            confidence={
                "effect_size": result.effect_ci,
                "group_a.win_rate": sum_a.win_rate_ci,
                "group_b.win_rate": sum_b.win_rate_ci,
            },
            caveats=caveats,
            source=["opendota"],
            data_freshness=an.newest_match(rows),
        )

    @mcp.tool
    async def analyze_correlation(
        metric: Annotated[
            str,
            Field(
                description=(
                    "Per-player metric to relate to winning: gpm, xpm, kills, deaths, "
                    "assists, last_hits, hero_damage, tower_damage, hero_healing, net_worth, "
                    "wards (obs_placed), sen_placed, stacks (camps_stacked), rune_pickups, "
                    "stuns, teamfight_participation."
                )
            ),
        ],
        hero: Annotated[
            str | None,
            Field(description=f"Limit to one hero's players; omit for every player. {args.HERO}"),
        ] = None,
        filters: FiltersArg = None,
    ) -> ToolResult[an.CorrelationResult]:
        """How strongly a metric (GPM, wards, stacks...) is associated with winning, with
        match duration controlled for: games are split into duration strata and, within each,
        into players above vs at-or-below the stratum's median. Reports the Mantel-Haenszel
        odds ratio with its CI and CMH p-value, per-stratum win rates and correlations, and a
        selection-bias warning."""
        name = an.metric_name(metric)
        if name is None or name in ("duration_min", "lane_role"):
            return error(
                f"Unknown metric {metric!r}.",
                hint="Use one of: "
                + ", ".join(m for m in an.PLAYER_METRICS if m not in ("duration_min", "lane_role")),
            )
        found = None
        if hero is not None:
            found = _hero(hero)
            if isinstance(found, ToolResult):
                return found
        league = _league_filters(filters or Filters())
        if isinstance(league, ToolResult):
            return league
        used, caveats = league
        rows = await _rows(an.player_rows_sql(found.id if found else None, used.patch))
        if isinstance(rows, ToolResult):
            return rows
        caveats = [SELECTION_CAVEAT, CAUSALITY_CAVEAT, *caveats]
        strata, tables, values, wins = an.correlate_with_win(rows, name)
        n = sum(t.n for t in tables)
        who = found.display_name if found else "all heroes"
        if n < used.min_sample or not tables:
            return insufficient_data(
                f"Only {n} usable parsed league player rows for {who} on patch {used.patch}.",
                "Drop the hero, use an older patch, or lower min_sample.",
                filters=used,
                sample_size=n,
                caveats=caveats,
                source=["opendota"],
            )
        mh = an.mantel_haenszel(tables)
        direction = "none"
        if mh.p_value < an.ALPHA and mh.odds_ratio is not None:
            direction = "positive" if mh.odds_ratio > 1 else "negative"
        odds = f"{mh.odds_ratio:.2f}" if mh.odds_ratio is not None else "undefined"
        if direction == "none":
            text = (
                f"No reliable association of {name} with winning once duration is held "
                f"fixed (odds ratio {odds}, p = {mh.p_value:.3g})."
            )
        else:
            text = (
                f"Players above their duration stratum's median {name} have {odds}x the odds "
                f"of winning of those at or below it (p = {mh.p_value:.3g}). This is an "
                "association; see caveats for why it is not a causal effect."
            )
        if not found:
            caveats.append(
                "All heroes and roles are pooled, so the metric mixes cores and supports; "
                "pass a hero for a cleaner comparison."
            )
        skipped = [s.label for s in strata if not s.used]
        if skipped:
            caveats.append(
                f"Duration strata {', '.join(skipped)} had fewer than {an.MIN_STRATUM} games or "
                "no spread in the metric and were left out of the pooled estimate."
            )
        result = an.CorrelationResult(
            hero=found.display_name if found else None,
            patch=used.patch,
            metric=name,
            games=n,
            metric_median=median(values),
            unadjusted_correlation=an.point_biserial(values, wins),
            unadjusted_odds_ratio=an.crude_odds_ratio(tables),
            adjusted_odds_ratio=mh.odds_ratio,
            adjusted_odds_ratio_ci=mh.ci,
            p_value=mh.p_value,
            strata=strata,
            direction=direction,
            interpretation=text,
        )
        confidence = {"adjusted_odds_ratio": mh.ci} if mh.ci else {}
        return ok(
            result,
            filters=used,
            sample_size=n,
            period=_period(rows),
            definitions={
                name: an.PLAYER_METRICS[name][1],
                "strata": (
                    "Match duration strata in minutes: <25, 25-35, 35-45, 45+. Within each, "
                    "'high' is a metric above the stratum's median, 'low' at or below it."
                ),
                "adjusted_odds_ratio": (
                    "Mantel-Haenszel common odds ratio of winning, high vs low half, across "
                    "duration strata; 95% CI by Robins-Breslow-Greenland. 1 = no association."
                ),
                "p_value": "Cochran-Mantel-Haenszel chi-square test (continuity corrected).",
                "unadjusted_correlation": "Point-biserial r of the raw metric and win.",
                "unadjusted_odds_ratio": "Odds ratio of the summed strata tables (crude).",
            },
            confidence=confidence,
            caveats=caveats,
            source=["opendota"],
            data_freshness=an.newest_match(rows),
        )

    @mcp.tool
    async def find_matches(
        heroes: Annotated[
            list[str] | None,
            Field(description=f"Heroes that must all be on the same team. {args.HEROES}"),
        ] = None,
        enemy_heroes: Annotated[
            list[str] | None,
            Field(description=f"Heroes that must all be on the opposing team. {args.HEROES}"),
        ] = None,
        heroes_outcome: Annotated[
            Outcome,
            Field(description='"win"/"loss" for matches the `heroes` team won/lost; "all".'),
        ] = Outcome.ALL,
        min_duration_min: Annotated[int | None, Field(ge=0, le=200)] = None,
        max_duration_min: Annotated[int | None, Field(ge=0, le=200)] = None,
        min_comeback_gold: Annotated[
            int | None,
            Field(
                ge=1000,
                le=100_000,
                description=(
                    "Only matches whose winner was at least this much gold behind at some "
                    "minute (e.g. 20000 for comebacks from 20k behind). League matches only."
                ),
            ),
        ] = None,
        source: Annotated[
            Literal["auto", "pub", "league"],
            Field(
                description=(
                    '"pub": sampled public matches (heroes, rank, duration). "league": parsed '
                    'league matches (adds gold swings and team names). "auto" picks league '
                    "when min_comeback_gold is set, else pub."
                )
            ),
        ] = "auto",
        filters: Annotated[
            Filters | None,
            Field(
                description=(
                    "patch, rank_bracket (pubs only) and game_mode (default ranked_all_pick "
                    "for pubs). position and min_sample do not apply to a search."
                )
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=an.FIND_MAX_LIMIT)] = an.FIND_DEFAULT_LIMIT,
        before_match_id: Annotated[
            int | None,
            Field(
                gt=0,
                description="Pagination cursor: pass next_before_match_id of the previous page.",
            ),
        ] = None,
    ) -> ToolResult[dict[str, Any]]:
        """Find concrete matches by heroes (allies and enemies), outcome, rank, duration,
        patch and net-worth swing (e.g. comebacks from 20k gold behind), newest first, with
        pagination. Returns match ids, heroes, winner and OpenDota links."""
        requested = filters or Filters()
        kind = source if source != "auto" else ("league" if min_comeback_gold else "pub")
        if min_comeback_gold is not None and kind == "pub":
            return error(
                "Gold swings are only recorded for parsed league matches.",
                hint='Use source "league" or "auto", or drop min_comeback_gold.',
            )
        if (
            min_duration_min is not None
            and max_duration_min is not None
            and min_duration_min > max_duration_min
        ):
            return error("min_duration_min is above max_duration_min.")
        ids: dict[str, list[int]] = {"heroes": [], "enemy_heroes": []}
        for key, names in (("heroes", heroes), ("enemy_heroes", enemy_heroes)):
            for name in names or []:
                found = _hero(name)
                if isinstance(found, ToolResult):
                    return found
                ids[key].append(found.id)
        if len(set(ids["heroes"])) > 5 or len(set(ids["enemy_heroes"])) > 5:
            return error("A team has at most 5 heroes.")
        if set(ids["heroes"]) & set(ids["enemy_heroes"]):
            return error("A hero cannot be on both teams.")
        try:
            patch = explorer_patch(requested) if requested.patch or kind == "league" else None
        except ValueError as exc:
            return error(str(exc), filters=requested)
        caveats: list[str] = []
        if heroes_outcome is not Outcome.ALL and not ids["heroes"]:
            caveats.append("heroes_outcome needs heroes; it was ignored.")
        search_args: dict[str, Any] = {
            "heroes": tuple(ids["heroes"]),
            "enemy_heroes": tuple(ids["enemy_heroes"]),
            "heroes_win": None if heroes_outcome is Outcome.ALL else heroes_outcome is Outcome.WIN,
            "min_duration_s": min_duration_min * 60 if min_duration_min is not None else None,
            "max_duration_s": max_duration_min * 60 if max_duration_min is not None else None,
            "before_match_id": before_match_id,
            "limit": limit,
            "min_comeback_gold": min_comeback_gold,
            "patch": patch,
        }
        if kind == "pub":
            search_args["game_mode"] = requested.game_mode or GameMode.RANKED_ALL_PICK
            if requested.rank_range:
                search_args["rank_tiers"] = an.rank_tiers(*requested.rank_range)
                if requested.rank_range[1].tier >= 7:
                    caveats.append(
                        "OpenDota's avg_rank_tier stops at 75, so Immortal players count as "
                        "Divine and an immortal-only filter matches almost nothing."
                    )
            if patch:
                try:
                    bounds = await an.patch_match_ids(patch)
                except OpenDotaError as exc:
                    return error(f"OpenDota explorer failed: {exc}", source=["opendota"])
                if bounds is None:
                    return error(f"No matches recorded for patch {patch}.", filters=requested)
                newest = patch == patches()[-1].name
                search_args["patch_ids"] = (bounds[0], None if newest else bounds[1])
                caveats.append(
                    "The patch is matched by match-id range (taken from league matches of "
                    "that patch), so a few matches at the patch boundary may be off."
                )
            else:
                caveats.append(
                    f"Without a patch only the newest {an.PUB_DEFAULT_WINDOW:,} match ids "
                    "(about the last few days) of public matches are searched."
                )
            caveats.append("Public matches are OpenDota's sample of pubs, not every match played.")
        else:
            if requested.game_mode:
                search_args["game_mode"] = requested.game_mode
            caveats.append(LEAGUE_CAVEAT)
            if requested.rank_bracket:
                caveats.append("League matches have no medal; rank_bracket was ignored.")
        search = an.MatchSearch(source=kind, **search_args)
        rows = await _rows(an.find_matches_sql(search))
        if isinstance(rows, ToolResult):
            return rows
        names = {h.id: h.display_name for h in hero_by_id().values()}
        page = rows[:limit]
        if not page:
            return insufficient_data(
                "No matches fit these conditions.",
                "Widen the duration range, drop a hero or the comeback threshold, or use "
                "another patch.",
                sample_size=0,
                caveats=caveats,
                source=["opendota"],
            )
        found_matches = [an.found_match(r, names) for r in page]
        next_cursor = page[-1]["match_id"] if len(rows) > limit else None
        applied = {
            "source": kind,
            "heroes": [names[i] for i in ids["heroes"]],
            "enemy_heroes": [names[i] for i in ids["enemy_heroes"]],
            "heroes_outcome": heroes_outcome.value,
            "min_duration_min": min_duration_min,
            "max_duration_min": max_duration_min,
            "min_comeback_gold": min_comeback_gold,
            "patch": patch,
            "rank_bracket": requested.rank_bracket if kind == "pub" else None,
            "game_mode": search.game_mode.value if search.game_mode else None,
        }
        return ok(
            {
                "matches": [m.model_dump(mode="json") for m in found_matches],
                "next_before_match_id": next_cursor,
                "applied": applied,
            },
            sample_size=len(found_matches),
            period=_period(page),
            definitions={
                "winner_max_deficit": (
                    "From radiant_gold_adv: the largest team gold deficit the eventual winner "
                    "had at any minute."
                ),
                "avg_rank_tier": "OpenDota average medal tier: tens digit medal, ones star.",
                "next_before_match_id": "Cursor for the next page; null on the last page.",
            },
            caveats=caveats,
            source=["opendota"],
            data_freshness=an.newest_match(page),
        )

    @mcp.tool
    async def explore_query(
        table: Annotated[
            Literal["public_matches", "matches", "player_matches"],
            Field(description="Table to query. " + _FIELD_LIST),
        ],
        select: Annotated[
            list[str] | None,
            Field(description="Fields to return (without aggregates). Default match_id."),
        ] = None,
        where: Annotated[
            list[an.ExploreFilter] | None,
            Field(
                description=(
                    'Filters, all must hold. Example: [{"field": "hero_id", "op": "=", '
                    '"value": 44}, {"field": "duration_min", "op": ">=", "value": 40}].'
                )
            ),
        ] = None,
        group_by: Annotated[list[str] | None, Field(description="Fields to group by.")] = None,
        aggregates: Annotated[
            list[an.ExploreAggregate] | None,
            Field(
                description=(
                    'Aggregates per group, e.g. [{"func": "avg", "field": "win"}] for a win '
                    "rate. A row count n is always added when aggregating."
                )
            ),
        ] = None,
        order_by: Annotated[
            str | None, Field(description="Output column to sort by (field or alias).")
        ] = None,
        descending: bool = True,
        limit: Annotated[
            int, Field(ge=1, le=an.EXPLORE_MAX_LIMIT, description="Maximum rows returned.")
        ] = an.EXPLORE_DEFAULT_LIMIT,
        patch: Annotated[
            str | None,
            Field(description='matches / player_matches: patch such as "7.41". Default current.'),
        ] = None,
        recent_match_window: Annotated[
            int,
            Field(
                ge=1,
                le=an.PUB_EXPLORE_MAX_WINDOW,
                description=(
                    "public_matches without a match_id filter: scan only the newest this many "
                    "match ids (100,000 is about 3 hours of pubs). Keeps the query inside the "
                    "explorer's time limit."
                ),
            ),
        ] = an.PUB_EXPLORE_DEFAULT_WINDOW,
        min_sample: Annotated[
            int,
            Field(ge=1, description="Fewest matching rows (sum of n when aggregating)."),
        ] = 30,
    ) -> ToolResult[dict[str, Any]]:
        """Ad-hoc question the other tools do not cover? Build a read-only query over the
        OpenDota explorer from structured parts: a whitelisted table, fields, filters,
        group-by and aggregates (count, avg, sum, min, max, median, stddev). Raw SQL is never
        accepted; the tool writes it from validated parts and returns it for transparency.
        Example: Phantom Assassin win rate by duration: table player_matches, where hero_id =
        44, group_by duration_min, aggregates avg(win)."""
        league = table != "public_matches"
        used_patch = None
        if league:
            try:
                used_patch = explorer_patch(Filters(patch=patch))
            except ValueError as exc:
                return error(str(exc))
        try:
            spec = an.ExploreSpec(
                table=table,
                select=select or [],
                filters=where or [],
                group_by=group_by or [],
                aggregates=aggregates or [],
                order_by=order_by,
                descending=descending,
                limit=limit,
                patch=used_patch,
                recent_match_window=recent_match_window,
            )
            built = an.build_explore_sql(spec)
        except ValueError as exc:
            return error(f"Invalid query: {exc}", hint="Fix the named part and retry.")
        rows = await _rows(built.sql)
        if isinstance(rows, ToolResult):
            return rows
        n = sum(int(r.get("n") or 0) for r in rows) if built.aggregated else len(rows)
        caveats = [*built.applied]
        caveats.append(
            LEAGUE_CAVEAT
            if league
            else (
                "public_matches is OpenDota's sample of public matches of the last year; it has "
                "no per-player stats."
            )
        )
        if len(rows) == limit:
            caveats.append(f"The result hit the row limit ({limit}); more rows may exist.")
        if n < min_sample:
            return insufficient_data(
                f"The query matched {n} rows, below min_sample {min_sample}.",
                "Loosen the filters, widen recent_match_window, or lower min_sample.",
                sample_size=n,
                caveats=caveats,
                source=["opendota"],
            )
        definitions = {
            name: an.EXPLORE_TABLES[table].fields[name].description
            for name in built.columns
            if name in an.EXPLORE_TABLES[table].fields
        }
        if built.aggregated:
            definitions["n"] = "Rows in the group."
            caveats.append(
                "Aggregates over small groups are noisy: check n before comparing groups, "
                "and remember avg(win) is a correlational win rate."
            )
        return ok(
            {"sql": built.sql, "columns": built.columns, "rows": rows, "patch": used_patch},
            sample_size=n,
            definitions=definitions,
            caveats=caveats,
            source=["opendota"],
        )
