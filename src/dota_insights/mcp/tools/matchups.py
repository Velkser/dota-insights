"""Hero matchup and draft tools: counters, synergies, combos, pick suggestions, draft analysis."""

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import matchups as mu
from dota_insights.mcp import args
from dota_insights.mcp.envelope import ToolResult, error, insufficient_data, ok
from dota_insights.models import Filters, Position
from dota_insights.reference import hero_by_id, patches, resolve_hero

FiltersArg = Annotated[
    Filters | None,
    Field(
        description=(
            "Match filters. rank_bracket is supported (STRATZ groups medals in pairs). "
            "Matchup pairs are not split by position; patch must be omitted or the current "
            "patch; game_mode must be omitted. min_sample is the fewest games a hero pair "
            "needs to be used (default 100)."
        )
    ),
]
HeroList = Annotated[
    list[str],
    Field(description=args.HEROES),
]

DEFINITIONS = {
    "win_rate": "Wins / games of the subject hero in games with that pair (against or with).",
    "base_win_rate": "The hero's overall win rate over the same bracket and period.",
    "matchup_advantage": (
        "Win rate against the opponent minus the log5 expectation a(1-b)/(a(1-b)+b(1-a)) "
        "from the two base win rates, in win-rate points (0.02 = 2 points). Negative: the "
        "opponent counters the hero."
    ),
    "synergy_advantage": (
        "Win rate with the ally minus ab/(ab+(1-a)(1-b)), the win rate the two base rates "
        "predict if the heroes did not interact. Positive: they win more together."
    ),
    "stratz_synergy": "STRATZ's own pair score, shown for reference; not used in rankings.",
    "win_rate_ci": "95% Wilson interval of the pair win rate.",
}

STRATZ_CAVEAT = (
    "Matchup data from STRATZ (https://stratz.com), over its recent match window; STRATZ does "
    "not state the exact dates or game modes of this aggregate."
)
CORRELATION_CAVEAT = (
    "Matchup and synergy advantages are correlational: draft context, player skill and "
    "position are not controlled for."
)
POSITION_CAVEAT = (
    "Matchup pairs are not split by position. Position filters choose heroes by how "
    f"often they are played there (at least {mu.MIN_POSITION_SHARE:.0%} of their games)."
)


def _resolve(names: list[str]) -> tuple[list[int], ToolResult | None]:
    ids: list[int] = []
    for name in names:
        res = resolve_hero(name)
        hero = res.best
        if hero is None:
            options = [c.record.display_name for c in res.candidates]
            if options:
                return [], error(
                    f"Hero {name!r} is ambiguous: {', '.join(options)}",
                    hint="Repeat the call with one of these names.",
                )
            return [], error(f"No hero matches {name!r}.", hint="Check the spelling.")
        if hero.id in ids:
            return [], error(f"{hero.display_name} is listed twice.")
        ids.append(hero.id)
    return ids, None


def _check_filters(filters: Filters) -> ToolResult | None:
    current = patches()[-1].name
    if filters.patch and not filters.patch.startswith(current):
        return error(
            f"Matchup data covers only the current patch ({current}); patch "
            f"{filters.patch!r} is not available.",
            hint="Omit patch.",
            filters=filters,
        )
    if filters.game_mode is not None:
        return error(
            "The matchup source cannot filter by game mode.",
            hint="Omit game_mode.",
            filters=filters,
        )
    return None


def _caveats(filters: Filters, data: mu.MatchupData, *extra: str) -> list[str]:
    out = []
    if data.scope is not None:
        out.extend(data.scope.caveats())
    elif data.source == "stratz":
        out.append(STRATZ_CAVEAT)
    else:
        out.append(
            "STRATZ was unavailable, so opponents come from OpenDota /heroes/{id}/matchups: "
            "small samples, all ranks, no ally data, base win rates from OpenDota /heroStats."
        )
    if note := mu.bracket_caveat(filters, data.brackets):
        out.append(note)
    out.append(CORRELATION_CAVEAT)
    out.extend(extra)
    return out


def _source(data: mu.MatchupData) -> list[Any]:
    return data.sources


async def _positions_or_error(
    filters: Filters, needed: bool
) -> tuple[mu.PositionStats | None, ToolResult | None]:
    positions = await mu.load_positions(filters)
    if needed and positions is None:
        return None, error(
            "Position data comes from STRATZ, which is unavailable right now.",
            hint="Retry without position filters.",
            filters=filters,
        )
    return positions, None


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_hero_matchups(
        hero: Annotated[str, Field(description=args.HERO)],
        filters: FiltersArg = None,
        mode: Annotated[
            Literal["counters", "synergies", "both"],
            Field(
                description=(
                    '"counters": heroes that beat this hero and heroes it beats. "synergies": '
                    'best and worst allies. "both": all four lists.'
                )
            ),
        ] = "both",
        other_position: Annotated[
            Position | None,
            Field(
                description=(
                    "Only list heroes commonly played at this position (1 carry ... 5 hard "
                    'support), e.g. 5 for "which supports counter Anti-Mage".'
                )
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Heroes per list.")] = 10,
    ) -> ToolResult[dict]:
        """Win rate and matchup advantage of a hero against and alongside every other hero.

        Lists are ranked by advantage over what the two heroes' base win rates predict, so a
        strong hero is not shown as a counter merely for being strong.
        """
        filters = filters or Filters()
        if (bad := _check_filters(filters)) is not None:
            return bad
        ids, bad = _resolve([hero])
        if bad is not None:
            return bad
        hero_id = ids[0]
        try:
            data = await mu.load_matchups([hero_id], filters)
        except mu.MatchupsUnavailable as exc:
            return error(str(exc), hint="Retry later.", filters=filters)
        positions, bad = await _positions_or_error(
            filters, other_position is not None or filters.position is not None
        )
        if bad is not None:
            return bad

        games = data.games.get(hero_id, 0)
        name = mu.hero_name(hero_id)
        if games < filters.min_sample:
            return insufficient_data(
                f"{name} has {games} games for these filters (min_sample {filters.min_sample}).",
                "Widen rank_bracket or lower min_sample.",
                filters=filters,
                sample_size=games,
                source=_source(data),
                period=data.period,
                data_freshness=data.freshness,
            )
        others = positions.heroes_at(other_position) if other_position and positions else None
        result = mu.hero_matchups(
            data, hero_id, mode=mode, min_pair_games=filters.min_sample, limit=limit, others=others
        )
        lists = [
            k
            for k in ("counters", "easiest_matchups", "best_allies", "worst_allies")
            if k in result
        ]
        if not any(result[k] for k in lists):
            return insufficient_data(
                f"No hero pair with {name} reaches {filters.min_sample} games.",
                "Widen rank_bracket, drop other_position or lower min_sample.",
                filters=filters,
                sample_size=games,
                source=_source(data),
                period=data.period,
                data_freshness=data.freshness,
            )
        result = {
            "hero": name,
            "hero_id": hero_id,
            "games": games,
            "base_win_rate": round(data.base.get(hero_id, 0.0), 4),
            **result,
        }
        extra = []
        if filters.position is not None and positions is not None:
            pos = int(filters.position)
            pg = positions.games.get(hero_id, {}).get(pos, 0)
            pw = positions.wins.get(hero_id, {}).get(pos, 0)
            result["at_position"] = {
                "position": pos,
                "games": pg,
                "share": round(positions.share(hero_id, pos), 4),
                "win_rate": round(pw / pg, 4) if pg else None,
            }
            extra.append(
                f"Pairs are over all of {name}'s positions; at_position shows the "
                "hero's own win rate at the requested position only."
            )
        if not data.has_synergy and mode != "counters":
            extra.append("Ally synergies are unavailable from the fallback source.")
        if other_position is not None:
            extra.append(POSITION_CAVEAT)
        confidence = {
            f"{k}.{e['hero']}.win_rate": tuple(e["win_rate_ci"])
            for k in lists
            for e in result[k][:3]
        }
        return ok(
            result,
            filters=filters,
            sample_size=games,
            definitions={
                k: DEFINITIONS[k]
                for k in (
                    "win_rate",
                    "base_win_rate",
                    "matchup_advantage",
                    "synergy_advantage",
                    "stratz_synergy",
                    "win_rate_ci",
                )
            }
            | {
                "advantage": "matchup_advantage in counters/easiest_matchups, "
                "synergy_advantage in best_allies/worst_allies; from the "
                "subject hero's side.",
                "games": "Games of the subject hero (pair counts summed over five opponents / 5).",
            },
            confidence=confidence,
            caveats=_caveats(filters, data, *extra),
            source=_source(data),
            period=data.period,
            data_freshness=data.freshness,
        )

    @mcp.tool
    async def find_hero_combos(
        filters: FiltersArg = None,
        size: Annotated[Literal[2, 3], Field(description="2 for duos, 3 for trios.")] = 2,
        positions: Annotated[
            list[Position] | None,
            Field(
                description=(
                    "One position per slot, e.g. [1, 5] for carry + hard support duos. Each "
                    "hero must be commonly played at its slot's position. Omit for any heroes."
                )
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Combos to return.")] = 10,
    ) -> ToolResult[dict]:
        """Best ally duos or trios by synergy advantage, with per-pair sample thresholds."""
        filters = filters or Filters()
        if (bad := _check_filters(filters)) is not None:
            return bad
        if positions is not None and len(positions) != size:
            return error(f"positions needs exactly {size} entries, got {len(positions)}.")
        try:
            data = await mu.load_matchups(None, filters)
        except mu.MatchupsUnavailable as exc:
            return error(
                str(exc), hint="Combos need STRATZ ally data; retry later.", filters=filters
            )
        pos_stats, bad = await _positions_or_error(filters, positions is not None)
        if bad is not None:
            return bad
        slots = [pos_stats.heroes_at(p) for p in positions] if positions and pos_stats else None
        combos = mu.find_combos(
            data, size=size, slots=slots, min_pair_games=filters.min_sample, limit=limit
        )
        if not combos:
            return insufficient_data(
                f"No {size}-hero combo has every pair at {filters.min_sample}+ games.",
                "Widen rank_bracket, drop positions or lower min_sample.",
                filters=filters,
                source=_source(data),
                period=data.period,
                data_freshness=data.freshness,
            )
        extra = [POSITION_CAVEAT] if positions else []
        if size == 3:
            extra.append(
                "Trio scores are the sum of the three pair synergy advantages; "
                "STRATZ has no three-hero aggregates, so trios are not observed as such."
            )
        confidence = {
            f"{' + '.join(c['heroes'])}.win_rate": tuple(c["win_rate_ci"])
            for c in combos[:5]
            if "win_rate_ci" in c
        }
        return ok(
            {"size": size, "positions": [int(p) for p in positions or []], "combos": combos},
            filters=filters,
            sample_size=min(c["min_pair_games"] for c in combos),
            definitions={
                "synergy_advantage": DEFINITIONS["synergy_advantage"] + " Summed over pairs.",
                "min_pair_games": "Fewest games among the combo's hero pairs.",
                "win_rate": "Duo only: win rate in games the two heroes played together.",
                "win_rate_ci": DEFINITIONS["win_rate_ci"],
            },
            confidence=confidence,
            caveats=_caveats(filters, data, *extra),
            source=_source(data),
            period=data.period,
            data_freshness=data.freshness,
        )

    @mcp.tool
    async def suggest_pick(
        allies: HeroList,
        enemies: HeroList,
        bans: Annotated[
            list[str] | None, Field(description=f"Banned heroes to exclude. {args.HEROES}")
        ] = None,
        position: Annotated[
            Position | None,
            Field(description="Position of the open slot; candidates must be played there."),
        ] = None,
        filters: FiltersArg = None,
        player_pool: Annotated[
            list[str] | None,
            Field(description="Only consider these heroes (the player's hero pool)."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=30, description="Candidates to return.")] = 10,
    ) -> ToolResult[dict]:
        """Rank heroes for the open slot by matchups against enemies and synergy with allies.

        Score = sum of the candidate's matchup advantages against each enemy plus its synergy
        advantages with each ally (pairs below min_sample are skipped and listed).
        """
        filters = filters or Filters()
        if (bad := _check_filters(filters)) is not None:
            return bad
        if len(allies) > 4 or len(enemies) > 5:
            return error("At most 4 allies and 5 enemies.")
        if not allies and not enemies:
            return error("Give at least one ally or enemy.")
        ally_ids, bad = _resolve(allies)
        if bad is not None:
            return bad
        enemy_ids, bad = _resolve(enemies)
        if bad is not None:
            return bad
        ban_ids, bad = _resolve(bans or [])
        if bad is not None:
            return bad
        taken = set(ally_ids) | set(enemy_ids) | set(ban_ids)
        if len(taken) < len(ally_ids) + len(enemy_ids) + len(ban_ids):
            return error("A hero appears in more than one of allies, enemies and bans.")
        candidates = set(hero_by_id()) - taken
        if player_pool:
            pool_ids, bad = _resolve(player_pool)
            if bad is not None:
                return bad
            candidates &= set(pool_ids)
        try:
            data = await mu.load_matchups(ally_ids + enemy_ids, filters)
        except mu.MatchupsUnavailable as exc:
            return error(str(exc), hint="Retry later.", filters=filters)
        pos_stats, bad = await _positions_or_error(filters, position is not None)
        if bad is not None:
            return bad
        if position is not None and pos_stats is not None:
            candidates &= pos_stats.heroes_at(position)
        if not candidates:
            return error(
                "No candidate heroes left after allies, enemies, bans, pool and position.",
                filters=filters,
            )
        rows = mu.suggest_picks(
            data,
            allies=ally_ids,
            enemies=enemy_ids,
            candidates=candidates,
            min_pair_games=filters.min_sample,
            limit=limit,
        )
        if not rows:
            return insufficient_data(
                f"No candidate has a pair with these heroes at {filters.min_sample}+ games.",
                "Widen rank_bracket or lower min_sample.",
                filters=filters,
                source=_source(data),
                period=data.period,
                data_freshness=data.freshness,
            )
        # Smallest pair behind any returned score.
        smallest = min(
            p.games
            for r in rows
            for p in [
                *(data.against(r["hero_id"], e) for e in enemy_ids),
                *(data.alongside(r["hero_id"], a) for a in ally_ids),
            ]
            if p is not None and p.games >= filters.min_sample
        )
        extra = [
            "The score adds pair effects as if they were independent; it is a ranking "
            "heuristic, not a predicted win rate. Missing pairs count as zero.",
        ]
        if position is not None:
            extra.append(POSITION_CAVEAT)
        if not data.has_synergy and ally_ids:
            extra.append("Ally synergies are unavailable from the fallback source.")
        return ok(
            {
                "allies": [mu.hero_name(h) for h in ally_ids],
                "enemies": [mu.hero_name(h) for h in enemy_ids],
                "position": int(position) if position else None,
                "candidates_considered": len(candidates),
                "suggestions": rows,
            },
            filters=filters,
            sample_size=smallest,
            definitions={
                "score": "matchup_advantage_sum + synergy_advantage_sum, in win-rate points.",
                "matchup_advantage_sum": "Sum over enemies of " + DEFINITIONS["matchup_advantage"],
                "synergy_advantage_sum": "Sum over allies of " + DEFINITIONS["synergy_advantage"],
                "reasons": "The candidate's largest pair effects by absolute size.",
                "pairs_missing": "Pairs skipped for having fewer than min_sample games.",
            },
            caveats=_caveats(filters, data, *extra),
            source=_source(data),
            period=data.period,
            data_freshness=data.freshness,
        )

    @mcp.tool
    async def analyze_draft(
        radiant: HeroList,
        dire: HeroList,
        filters: FiltersArg = None,
    ) -> ToolResult[dict]:
        """Per-hero and pairwise matchup/synergy contributions of a draft, a heuristic Radiant
        win estimate, and weaknesses visible in the data (counters, poor pairs, uncovered
        positions, missing Disabler/Initiator tags)."""
        filters = filters or Filters()
        if (bad := _check_filters(filters)) is not None:
            return bad
        if not 1 <= len(radiant) <= 5 or not 1 <= len(dire) <= 5:
            return error("Each side needs 1 to 5 heroes.")
        rad, bad = _resolve(radiant)
        if bad is not None:
            return bad
        dire_ids, bad = _resolve(dire)
        if bad is not None:
            return bad
        if set(rad) & set(dire_ids):
            return error("A hero cannot be on both sides.")
        try:
            data = await mu.load_matchups(rad + dire_ids, filters)
        except mu.MatchupsUnavailable as exc:
            return error(str(exc), hint="Retry later.", filters=filters)
        pos_stats = await mu.load_positions(filters)
        result = mu.analyze_draft(
            data, radiant=rad, dire=dire_ids, min_pair_games=filters.min_sample, positions=pos_stats
        )
        pairwise = result["pairwise"]
        games = [
            x["games"] for k in ("matchups", "radiant_synergy", "dire_synergy") for x in pairwise[k]
        ]
        if not games:
            return insufficient_data(
                f"No hero pair in this draft has {filters.min_sample}+ games.",
                "Widen rank_bracket or lower min_sample.",
                filters=filters,
                source=_source(data),
                period=data.period,
                data_freshness=data.freshness,
            )
        extra = [
            "radiant_win_estimate is a heuristic, not a calibrated model: 0.5 + the difference "
            "in mean base win rate + summed Radiant matchup advantages / 5 + (Radiant synergy "
            "- Dire synergy) / 5, clipped to 5-95%. It has no confidence interval and ignores "
            "players, lanes, timings and pairs below min_sample.",
            "Weaknesses only cover what the data shows: pair advantages beyond 2 points, "
            "positions no hero is commonly played at, and Valve's Disabler/Initiator role tags.",
        ]
        if pos_stats is None:
            extra.append("Position data was unavailable, so position coverage is not checked.")
        if len(rad) < 5 or len(dire_ids) < 5:
            extra.append("The draft is incomplete; the estimate covers the picked heroes only.")
        return ok(
            result,
            filters=filters,
            sample_size=min(games),
            definitions={
                "matchup_advantage_sum": "Per hero: sum over enemies of "
                + DEFINITIONS["matchup_advantage"],
                "synergy_advantage_sum": "Per hero: sum over allies of "
                + DEFINITIONS["synergy_advantage"],
                "radiant_advantage": "matchup_advantage from the Radiant hero's side.",
                "estimate_components": "Terms of radiant_win_estimate before clipping.",
                "usual_positions": f"Positions with at least {mu.MIN_POSITION_SHARE:.0%} of the "
                "hero's games (STRATZ position detection).",
            },
            caveats=_caveats(filters, data, *extra),
            source=_source(data),
            period=data.period,
            data_freshness=data.freshness,
        )
