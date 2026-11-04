"""Hero build tools: items, skills, talents, Aghanim's upgrades and neutral items."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import builds
from dota_insights.analytics.stats import wilson_interval
from dota_insights.mcp import args
from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.mcp.errors import retry_hint, source_error
from dota_insights.models.filters import Filters, GameMode
from dota_insights.reference import Hero, resolve_hero
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

HeroArg = Annotated[
    str,
    Field(description=args.HERO),
]
FiltersArg = Annotated[
    Filters | None,
    Field(
        description=(
            "Rank bracket and position narrow the data. Only the current patch is "
            "available; game_mode, if given, must be ranked_all_pick or all_pick."
        )
    ),
]

STRATZ_LINK = "Item, talent and skill data from STRATZ (https://stratz.com)."
CORRELATIONAL = (
    "Item, talent and skill win rates are correlational: items bought late and in long games "
    "show higher win rates because the player was already winning or the game went on, not "
    "because the item wins games."
)
GUIDE_CAVEAT = (
    "Skill orders and facets come from a sample of recent STRATZ guide matches (high-rank "
    "games STRATZ selected as guides; every sampled one was a win), so they show what "
    "winning players did and carry no win rate. Guides are not filtered by rank bracket."
)
WINDOW_CAVEAT = (
    "STRATZ hero aggregates cover a recent rolling window that STRATZ does not date, so no "
    "period is reported; they reflect the current patch."
)
POPULARITY_CAVEAT = (
    "STRATZ gives no total match count with item rows; popularity is divided by the "
    "number of matches with a level-10 talent pick, which slightly overstates it."
)

BUILD_DEFINITIONS = {
    "reference_matches": (
        "Matches in which the hero picked a level-10 talent (or the most-bought item's "
        "count if larger); the denominator for popularity."
    ),
    "popularity": "Matches in which the item was bought at least once / reference_matches.",
    "win_rate": "Wins / matches among matches in which the item (or talent) was bought.",
    "median_minute": (
        "Median game minute of the first purchase, from STRATZ per-minute purchase counts."
    ),
    "typical_quantity": "Copies bought at the start in at least half the matches buying one.",
    "average_minute": "Mean game minute at which the boots were bought (STRATZ timeAverage).",
    "items.early": (
        f"Median purchase before minute {builds.EARLY_BEFORE_MINUTE} and popularity >= "
        f"{builds.EARLY_MIN_POPULARITY:.0%}; consumables and recipes excluded."
    ),
    "items.core": (
        f"Median purchase from minute {builds.EARLY_BEFORE_MINUTE} and popularity >= "
        f"{builds.CORE_MIN_POPULARITY:.0%}."
    ),
    "items.situational": (
        f"Median purchase from minute {builds.EARLY_BEFORE_MINUTE} and popularity "
        f"{builds.SITUATIONAL_MIN_POPULARITY:.0%}-{builds.CORE_MIN_POPULARITY:.0%}."
    ),
    "talents.pick_share": "Picks of this talent / picks of either talent at that level.",
    "skill_build.first_levels": (
        f"Most common order of the first {builds.SKILL_ORDER_LEVELS} skill points in guide "
        "matches, with share of guide matches."
    ),
    "skill_build.max_order": (
        "Order in which the basic abilities reached 4 points in guide matches."
    ),
    "facets": "Facet slot picked in guide matches (STRATZ variant; 0 means no facet, omitted).",
}


def _hero(query: str) -> Hero | ToolResult:
    resolution = resolve_hero(query)
    if resolution.best is not None:
        return resolution.best
    if not resolution.found:
        return error(f"No hero matches {query!r}.", hint="Check the spelling or use the full name.")
    names = ", ".join(c.record.display_name for c in resolution.candidates)
    return error(f"{query!r} matches several heroes: {names}.", hint="Pick one of them.")


def _check_filters(filters: Filters) -> ToolResult | None:
    current = builds.current_patch()
    if filters.patch and not filters.patch.startswith(current):
        return error(
            f"Only the current patch ({current}) is available; got patch {filters.patch!r}.",
            hint="Omit patch.",
            filters=filters,
        )
    if filters.game_mode not in (None, GameMode.RANKED_ALL_PICK, GameMode.ALL_PICK):
        return error(
            f"Build data is only available for all pick, not {filters.game_mode.value}.",
            hint="Omit game_mode.",
            filters=filters,
        )
    return None


def _bracket_caveats(filters: Filters) -> tuple[tuple[str, ...], list[str]]:
    brackets, tiers = builds.stratz_brackets(filters)
    caveats = []
    rank = filters.rank_range
    if rank and tiers and (rank[0].tier, rank[1].tier) != tiers:
        caveats.append(
            "STRATZ groups medals in pairs, so the rank filter was widened to "
            + ", ".join(b.lower() for b in brackets)
            + "."
        )
    return brackets, caveats


def _ci_entries(prefix: str, rows: list[dict[str, Any]], key: str = "item") -> dict:
    result = {}
    for r in rows:
        if r.get("win_rate_ci"):
            result[f"{prefix}.{r[key]}.win_rate"] = tuple(r["win_rate_ci"])
        if r.get("median_minute_ci"):
            result[f"{prefix}.{r[key]}.median_minute"] = tuple(r["median_minute_ci"])
    return result


def _guide_period(guides: tuple[builds.GuideMatch, ...]) -> Period | None:
    dates = [g.start.date() for g in guides if g.start]
    return Period(from_date=min(dates), to_date=max(dates)) if dates else None


async def _names(aggregates: dict[str, Any]) -> dict[int, str]:
    hero = (aggregates.get("constants") or {}).get("hero") or {}
    ids = tuple(sorted({t["abilityId"] for t in hero.get("talents") or []}))
    return await builds.fetch_ability_names(ids)


def _insufficient(n: int, filters: Filters, source: list) -> ToolResult:
    return insufficient_data(
        f"Only {n} matches for these filters (min_sample {filters.min_sample}).",
        "Widen rank_bracket (for example divine+ -> legend+), drop position, or lower min_sample.",
        filters=filters,
        sample_size=n,
        source=source,
    )


async def hero_builds(hero_query: str, filters: Filters | None) -> ToolResult:
    filters = filters or Filters()
    hero = _hero(hero_query)
    if isinstance(hero, ToolResult):
        return hero
    if (bad := _check_filters(filters)) is not None:
        return bad
    applied = filters.model_copy(update={"patch": builds.current_patch()})
    brackets, caveats = _bracket_caveats(filters)
    position = builds.stratz_position(filters.position)
    try:
        aggregates = await builds.fetch_aggregates(hero.id, brackets, position)
        names = await _names(aggregates)
        pool, guides = await builds.fetch_guides(hero.id, position)
    except StratzError as exc:
        return await _builds_fallback(hero, applied, f"STRATZ failed ({exc})")

    stats = aggregates.get("heroStats") or {}
    n = builds.reference_matches(aggregates)
    if n < filters.min_sample:
        return _insufficient(n, applied, ["stratz"])
    starting = builds.starting_items(stats.get("itemStartingPurchase") or [], n)
    boots = builds.boots(stats.get("itemBootPurchase") or [], n)
    phases = builds.item_phases(stats.get("itemFullPurchase") or [], n)
    talents = builds.talents(aggregates, names)
    result = {
        "hero": {"id": hero.id, "name": hero.display_name},
        "reference_matches": n,
        "starting_items": starting,
        "boots": boots,
        "items": phases,
        "talents": talents,
        "skill_build": builds.skill_orders(guides, aggregates, names),
        "facets": builds.facet_choices(guides, aggregates),
        "guide_pool": pool,
    }
    confidence = {
        **_ci_entries("starting_items", starting),
        **_ci_entries("boots", boots),
        **{
            k: v
            for phase, rows in phases.items()
            for k, v in _ci_entries(f"items.{phase}", rows).items()
        },
        **_ci_entries("talents", talents, key="talent"),
    }
    if not result["facets"]:
        caveats.append("No facet was recorded in the sampled guide matches.")
    if len(guides) < builds.GUIDE_LIMIT:
        caveats.append(f"Only {len(guides)} guide matches were available for the skill build.")
    if filters.position is None:
        caveats.append("All positions are mixed; set position for a role-specific build.")
    return ok(
        result,
        filters=applied,
        sample_size=n,
        definitions=BUILD_DEFINITIONS,
        confidence=confidence,
        caveats=[
            *caveats,
            CORRELATIONAL,
            POPULARITY_CAVEAT,
            GUIDE_CAVEAT,
            WINDOW_CAVEAT,
            STRATZ_LINK,
        ],
        source=["stratz"],
        data_freshness=aggregates.get("fetched_at"),
    )


async def _builds_fallback(hero: Hero, filters: Filters, reason: str) -> ToolResult:
    """OpenDota itemPopularity: pro-match item counts per phase, no win rates or filters."""
    applied = filters.model_copy(update={"rank_bracket": None, "position": None})
    try:
        raw = await builds.fetch_item_popularity(hero.id)
    except OpenDotaError as exc:
        return error(
            f"{reason}; OpenDota fallback failed ({exc}).",
            hint=retry_hint(exc),
            filters=filters,
            source=["stratz", "opendota"],
        )
    phases = builds.popularity_fallback(raw)
    n = max((r["count"] for r in phases.get("start_game") or []), default=0)
    if n < applied.min_sample:
        return _insufficient(n, applied, ["opendota"])
    return ok(
        {"hero": {"id": hero.id, "name": hero.display_name}, "item_counts": phases},
        filters=applied,
        sample_size=n,
        definitions={
            "count": "Times the item was bought in that game phase (OpenDota itemPopularity).",
            "sample_size": "Count of the most-bought starting item: a lower bound on matches.",
        },
        caveats=[
            f"{reason}, so this is OpenDota's fallback: item counts from professional "
            "matches only, with no win rates, timings, talents or skill builds, and no rank "
            "or position filter.",
            CORRELATIONAL,
        ],
        source=["opendota"],
        data_freshness=datetime.now(UTC),
    )


async def ability_stats(hero_query: str, filters: Filters | None) -> ToolResult:
    filters = filters or Filters()
    hero = _hero(hero_query)
    if isinstance(hero, ToolResult):
        return hero
    if (bad := _check_filters(filters)) is not None:
        return bad
    applied = filters.model_copy(update={"patch": builds.current_patch()})
    brackets, caveats = _bracket_caveats(filters)
    position = builds.stratz_position(filters.position)
    try:
        aggregates = await builds.fetch_aggregates(hero.id, brackets, position)
        names = await _names(aggregates)
        _, guides = await builds.fetch_guides(hero.id, position)
    except StratzError as exc:
        return source_error(exc, filters=applied, source=["stratz"])

    stats = aggregates.get("heroStats") or {}
    n = builds.reference_matches(aggregates)
    if n < filters.min_sample:
        return _insufficient(n, applied, ["stratz"])
    talents = builds.talents(aggregates, names)
    max_levels = builds.max_level_stats(aggregates)
    aghs = builds.aghanims(stats.get("itemFullPurchase") or [], n)
    confidence = _ci_entries("talents", talents, key="talent")
    for row in max_levels:
        for lvl in row["by_level_maxed"]:
            confidence[f"skills.{row['ability']}.maxed_at_{lvl['level']}.win_rate"] = tuple(
                lvl["win_rate_ci"]
            )
    for key, row in aghs.items():
        if row is None:
            caveats.append(
                f"STRATZ purchase data has no Aghanim's {key.title()} rows for this hero, so "
                "its purchase rate is unknown (STRATZ returned no Shard rows for any hero we "
                "checked, so Shard purchases may not be tracked there)."
            )
            continue
        confidence[f"aghanims.{key}.win_rate"] = tuple(row["win_rate_ci"])
        lo, hi = wilson_interval(row["matches"], n)
        confidence[f"aghanims.{key}.purchase_rate"] = (lo, hi)
        if row["median_minute_ci"]:
            confidence[f"aghanims.{key}.median_minute"] = tuple(row["median_minute_ci"])
        for bucket in row["by_timing"]:
            confidence[f"aghanims.{key}.minutes_{bucket['minutes']}.win_rate"] = tuple(
                bucket["win_rate_ci"]
            )
    result = {
        "hero": {"id": hero.id, "name": hero.display_name},
        "reference_matches": n,
        "skill_max_level": max_levels,
        "skill_orders": builds.skill_orders(guides, aggregates, names),
        "talents": talents,
        "aghanims": aghs,
    }
    return ok(
        result,
        filters=applied,
        sample_size=n,
        definitions={
            "reference_matches": BUILD_DEFINITIONS["reference_matches"],
            "skill_max_level": (
                "Per basic ability: matches in which it was maxed, the median hero level at "
                "which it reached max level, and win rate by that level (STRATZ "
                "abilityMaxLevel). An ability maxed at a low level was prioritised."
            ),
            "skill_orders": BUILD_DEFINITIONS["skill_build.max_order"]
            + " Also the most common first skill points. Guide matches only.",
            "talents.pick_share": BUILD_DEFINITIONS["talents.pick_share"],
            "aghanims.purchase_rate": "Matches buying it / reference_matches.",
            "aghanims.median_minute": BUILD_DEFINITIONS["median_minute"],
            "aghanims.by_timing": "Win rate by minute of purchase, buckets [lo, hi).",
            "win_rate": BUILD_DEFINITIONS["win_rate"],
        },
        confidence=confidence,
        caveats=[
            *caveats,
            CORRELATIONAL,
            "Win rate by purchase timing is confounded: early purchases happen in games the "
            "player is already winning.",
            "STRATZ has no aggregate of full skill orders; skill priority is shown by the level "
            "at which each ability was maxed.",
            POPULARITY_CAVEAT,
            GUIDE_CAVEAT,
            WINDOW_CAVEAT,
            STRATZ_LINK,
        ],
        source=["stratz"],
        data_freshness=aggregates.get("fetched_at"),
    )


def _best(rows: list[dict[str, Any]], min_matches: int) -> list[dict[str, Any]]:
    """Per tier, the item with the highest Wilson lower bound among those with enough games."""
    best: dict[Any, dict[str, Any]] = {}
    for r in rows:
        if r["matches"] < min_matches:
            continue
        current = best.get(r["tier"])
        if current is None or r["win_rate_ci"][0] > current["win_rate_ci"][0]:
            best[r["tier"]] = r
    return [best[t] for t in sorted(best, key=lambda t: t or 99)]


BEST_MIN_MATCHES = 20


async def neutral_items(
    filters: Filters | None, hero_query: str | None, tier: int | None
) -> ToolResult:
    filters = filters or Filters()
    hero = None
    if hero_query:
        hero = _hero(hero_query)
        if isinstance(hero, ToolResult):
            return hero
    if (bad := _check_filters(filters)) is not None:
        return bad
    patch = builds.current_patch()
    caveats: list[str] = []
    try:
        tiers = await builds.fetch_neutral_tiers()
    except OpenDotaError as exc:
        return source_error(exc, filters=filters, source=["opendota"])

    raw: list[tuple[int, int, int]] = []
    source: list = []
    period = None
    freshness = None
    applied = filters
    if hero is not None:
        brackets, caveats = _bracket_caveats(filters)
        try:
            rows = await builds.fetch_stratz_neutrals(
                hero.id, brackets, builds.stratz_position(filters.position)
            )
        except StratzError:
            rows = []
        if rows:
            raw = [(r["itemId"], r["matchCount"], r["winCount"]) for r in rows]
            source = ["stratz"]
            applied = filters.model_copy(update={"patch": patch})
            caveats += [WINDOW_CAVEAT, STRATZ_LINK]
            freshness = datetime.now(UTC)
    if not raw:
        try:
            rows = await builds.fetch_opendota_neutrals(patch, hero.id if hero else None)
        except (OpenDotaError, RuntimeError) as exc:
            return source_error(exc, filters=filters, source=["opendota"])
        raw = [(int(r["item_neutral"]), int(r["games"]), int(r["wins"])) for r in rows]
        source = ["opendota"]
        applied = filters.model_copy(
            update={"patch": patch, "rank_bracket": None, "position": None, "game_mode": None}
        )
        starts = [int(r["first_start"]) for r in rows] + [int(r["last_start"]) for r in rows]
        if starts:
            period = Period(
                from_date=datetime.fromtimestamp(min(starts), UTC).date(),
                to_date=datetime.fromtimestamp(max(starts), UTC).date(),
            )
            freshness = datetime.fromtimestamp(max(starts), UTC)
        caveats = [
            "Neutral item data comes from OpenDota league (professional and amateur league) "
            "matches of the current patch, because STRATZ returned no neutral item rows; "
            "rank bracket and position filters were not applied.",
            "The item is the neutral item held at the end of the match, so items swapped out "
            "earlier are not counted.",
        ]
        if filters.rank_bracket or filters.position:
            caveats.append("The requested rank_bracket/position could not be applied.")

    rows_out = builds.neutral_rows(raw, tiers, tier)
    n = sum(r["matches"] for r in rows_out)
    if n < filters.min_sample:
        return insufficient_data(
            f"Only {n} hero-matches with a neutral item for these filters "
            f"(min_sample {filters.min_sample}).",
            "Drop the hero or tier filter, or lower min_sample.",
            filters=applied,
            sample_size=n,
            period=period,
            source=source,
        )
    unknown = sum(r["matches"] for r in rows_out if r["tier"] is None)
    if unknown:
        caveats.append(f"{unknown} hero-matches had a neutral item with no known tier.")
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for r in rows_out:
        by_tier.setdefault(str(r["tier"]) if r["tier"] else "unknown", []).append(r)
    best = _best(rows_out, BEST_MIN_MATCHES) if hero is not None else []
    return ok(
        {
            "hero": {"id": hero.id, "name": hero.display_name} if hero else None,
            "tiers": by_tier,
            "best_for_hero": best,
        },
        filters=applied,
        sample_size=n,
        period=period,
        definitions={
            "matches": "Hero-matches that ended holding the item (one per player per match).",
            "share_of_tier": "Matches with the item / matches with any item of the same tier.",
            "win_rate": "Wins / matches among those holding the item.",
            "best_for_hero": (
                f"Per tier, the item with the highest 95% Wilson lower bound on win rate among "
                f"items with at least {BEST_MIN_MATCHES} matches."
            ),
            "tier": "Neutral item tier from OpenDota constants.",
        },
        confidence={f"{r['item']}.win_rate": tuple(r["win_rate_ci"]) for r in rows_out},
        caveats=[*caveats, CORRELATIONAL],
        source=source,
        data_freshness=freshness,
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_hero_builds(hero: HeroArg, filters: FiltersArg = None) -> ToolResult:
        """Item and skill build of a hero in the current patch.

        Starting items, boots, early/core/situational items with popularity, median purchase
        minute and win rate, talent picks per level, the usual skill order and facet choice.
        Filter by rank bracket and position. Item win rates are correlational.
        """
        return await hero_builds(hero, filters)

    @mcp.tool
    async def get_ability_stats(hero: HeroArg, filters: FiltersArg = None) -> ToolResult:
        """Skill priority, talents and Aghanim's upgrades of a hero in the current patch.

        Hero level at which each ability is maxed with win rate by that level, common skill
        orders, talent pick shares and win rates, and Aghanim's Scepter / Shard purchase
        rate, median minute and win rate by purchase timing.
        """
        return await ability_stats(hero, filters)

    @mcp.tool
    async def get_neutral_items(
        filters: FiltersArg = None,
        hero: Annotated[
            str | None,
            Field(description=f"Hero to get neutral items for; omit for all heroes. {args.HERO}"),
        ] = None,
        tier: Annotated[
            int | None, Field(ge=1, le=5, description="Neutral item tier 1-5. Omit for all.")
        ] = None,
    ) -> ToolResult:
        """Neutral item usage by tier with win rates, and the best neutral items for a hero."""
        return await neutral_items(filters, hero, tier)
