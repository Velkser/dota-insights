"""Economy tools: cost of dying, farm distribution, comebacks and objective impact."""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import economy, farming
from dota_insights.mcp.envelope import Period, Source, ToolResult, error, insufficient_data, ok
from dota_insights.models import Filters
from dota_insights.reference import Hero, resolve_hero
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

FiltersArg = Annotated[
    Filters | None,
    Field(
        description=(
            "Which matches to use. Only patch and min_sample apply to the league-match "
            "sample; omit for the current patch."
        )
    ),
]

LEAGUE_CAVEAT = (
    "Computed from the newest parsed league matches (pro and amateur leagues) in OpenDota's "
    "explorer, not ranked pubs: OpenDota only exposes gold timelines, objectives and death "
    "logs in bulk for league games, and only ~8% of pubs are parsed. Pub games are less "
    "coordinated, so expect pub numbers to differ."
)
CORRELATION_CAVEAT = (
    "Correlation, not causation: teams that get an objective or farm a certain way are "
    "often already winning, so a win rate does not say what doing it would change."
)
LEAD_DEFINITION = (
    "OpenDota radiant_gold_adv at the minute, from that team's side: its total net worth "
    "minus the enemy's (negative when behind)."
)


def _hero(query: str) -> Hero | ToolResult:
    resolution = resolve_hero(query)
    if resolution.best is not None:
        return resolution.best
    if not resolution.found:
        return error(f"No hero matches {query!r}.", hint="Check the spelling of the hero name.")
    names = ", ".join(c.record.display_name for c in resolution.candidates[:5])
    return error(f"{query!r} could match several heroes: {names}.", hint="Name one of them.")


def _league_filters(requested: Filters) -> tuple[Filters, list[str]] | ToolResult:
    """Filters the league sample can apply (patch, min_sample) and caveats for the rest."""
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
            f"League matches cannot be filtered by {', '.join(ignored)}; the league numbers "
            "cover all of them."
        )
    return Filters(patch=patch, min_sample=requested.min_sample), caveats


async def _sample(used: Filters, hero_id: int | None = None) -> economy.Sample | ToolResult:
    try:
        return await economy.fetch_sample(used.patch or "", hero_id)
    except OpenDotaError as exc:
        return error(f"OpenDota explorer failed: {exc}", filters=used, source=["opendota"])


def _period(sample: economy.Sample) -> Period | None:
    span = farming.match_period(sample.match_rows)
    return Period(from_date=span[0], to_date=span[1]) if span else None


def _not_enough(
    what: str,
    n: int,
    used: Filters,
    caveats: list[str],
    hint: str,
    sample: economy.Sample,
    source: list[Source] | None = None,
) -> ToolResult:
    return insufficient_data(
        f"Only {n} {what} in the {len(sample.matches)} newest parsed league matches on patch "
        f"{used.patch}.",
        hint,
        filters=used,
        sample_size=n,
        period=_period(sample),
        caveats=caveats,
        source=source or ["opendota"],
        data_freshness=farming.newest_match(sample.match_rows),
    )


def _rate_ci(name: str, r: economy.Rate) -> dict[str, tuple[float, float]]:
    return {name: (r.ci_low, r.ci_high)} if r.games else {}


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def analyze_death(
        hero: Annotated[
            str | None,
            Field(
                description=(
                    'Hero whose deaths to analyse, as a person would type it ("sf", '
                    '"Anti-Mage"). Omit for every hero.'
                )
            ),
        ] = None,
        later_minutes: Annotated[
            int,
            Field(
                ge=1,
                le=10,
                description="How many minutes after a death to measure the gold-lead swing.",
            ),
        ] = economy.DEFAULT_LATER_MINUTES,
        filters: FiltersArg = None,
    ) -> ToolResult[economy.DeathAnalysis]:
        """Cost of dying: gold lost and respawn time by game minute, how much the team's
        gold lead moves in the minutes after a death, and when buying back goes with
        winning (by phase and gold lead). Use for "how bad is dying at 30 minutes?" or
        "should I buy back?"."""
        found = None
        if hero is not None:
            found = _hero(hero)
            if isinstance(found, ToolResult):
                return found
        prepared = _league_filters(filters or Filters())
        if isinstance(prepared, ToolResult):
            return prepared
        used, caveats = prepared
        sample = await _sample(used, found.id if found else None)
        if isinstance(sample, ToolResult):
            return sample
        analysis = economy.death_analysis(
            used.patch or "", sample, found.id if found else None, later_minutes
        )
        who = f"deaths of {found.display_name}" if found else "hero deaths"
        if analysis.deaths < used.min_sample:
            return _not_enough(
                who,
                analysis.deaths,
                used,
                caveats,
                "Use an older patch or lower min_sample.",
                sample,
            )
        caveats += [
            "Respawn time and gold lost per bucket come only from 'clean' deaths: player-"
            "matches with exactly one death and no buyback, where the seconds dead and the "
            "death gold belong to that single death. They are few (see clean_deaths); a null "
            "median means none. Respawn by hero level is not available from the explorer.",
            "Deaths are read from the killers' kills_log, so deaths to creeps, towers, "
            "neutrals and denies are missing (~2% of deaths).",
            "The swing after a death includes everything else in those minutes (the fight "
            "the death was part of, trades, objectives), and teams that die more are often "
            "already losing.",
            "Buyback verdicts compare win rates after deaths with and without a buyback in "
            "the same phase and lead state. Players buy back when they expect a fight or a "
            "high-ground defence, so this is correlational, not the value of a buyback.",
        ]
        confidence: dict[str, tuple[float, float]] = {}
        for b in analysis.by_minute:
            if b.swing_ci_low is not None and b.swing_ci_high is not None:
                confidence[f"swing_after[{b.label}]"] = (b.swing_ci_low, b.swing_ci_high)
        for c in analysis.buybacks:
            key = f"{c.phase},{c.lead}"
            confidence.update(_rate_ci(f"buyback.win_rate[{key}]", c.buyback))
            confidence.update(_rate_ci(f"no_buyback.win_rate[{key}]", c.no_buyback))
        return ok(
            analysis,
            filters=used,
            sample_size=analysis.deaths,
            period=_period(sample),
            definitions={
                "deaths": "Hero deaths found in kills_log (sample_size counts these).",
                "gold_lost_per_death": (
                    "OpenDota gold_reasons death entry (gold lost on death) summed over "
                    "player-matches, divided by their deaths."
                ),
                "dead_seconds_per_death": (
                    "OpenDota life_state dead seconds summed, divided by deaths; buybacks "
                    "cut deaths short, so it is below the respawn timer."
                ),
                "respawn_s_median": "Median seconds dead of clean deaths in the bucket.",
                "gold_lost_median": "Median gold lost by clean deaths in the bucket.",
                "swing_after": (
                    f"Mean of the victim team's gold lead {later_minutes} minutes after the "
                    "death minute minus its lead at the death minute. Lead: " + LEAD_DEFINITION
                ),
                "bought_back": (
                    f"The victim bought back within {economy.BUYBACK_WINDOW_S} s of the death "
                    "and before dying again."
                ),
                "lead state": (
                    f"behind: lead <= -{economy.BUYBACK_LEAD}; ahead: >= "
                    f"{economy.BUYBACK_LEAD}; else even, at the death minute."
                ),
                "verdict": (
                    f'"pays off" / "does not pay off" if the win-rate difference has p < '
                    f'{economy.ALPHA} (two-proportion z-test), else "unclear".'
                ),
            },
            confidence=confidence,
            caveats=caveats,
            source=["opendota"],
            data_freshness=farming.newest_match(sample.match_rows),
        )

    @mcp.tool
    async def get_farm_distribution(
        minute: Annotated[
            Literal[10, 15, 20, 30] | None,
            Field(
                description=(
                    "Minute at which to measure each player's gold (10, 15, 20 or 30). "
                    "Omit for net worth at the end of the match."
                )
            ),
        ] = None,
        filters: FiltersArg = None,
    ) -> ToolResult[economy.FarmDistribution]:
        """Share of team gold held by positions 1-5, in wins vs losses, and which carry
        share / core share goes with winning. Use for "how much farm should the carry
        get?" or "do winning teams spread farm more?"."""
        prepared = _league_filters(filters or Filters())
        if isinstance(prepared, ToolResult):
            return prepared
        used, caveats = prepared
        sample = await _sample(used)
        if isinstance(sample, ToolResult):
            return sample
        dist = economy.farm_distribution(used.patch or "", sample, minute)
        if dist.teams < used.min_sample:
            return _not_enough(
                "teams with a 2-1-2 lane setup",
                dist.teams,
                used,
                caveats,
                "Measure at an earlier minute, use an older patch or lower min_sample.",
                sample,
            )
        caveats += [
            "Positions are inferred, not reported: OpenDota's lane_role gives the safe lane, "
            "mid and offlane; the richer player at minute 10 in the safe lane is position 1 "
            "and the other position 5, and likewise 3 and 4 in the offlane. Teams without "
            "two safe-lane, one mid and two offlane players are skipped (skipped_teams).",
            CORRELATION_CAVEAT,
        ]
        if minute is None:
            caveats.append(
                "Final net worth is partly a result of winning (the winners farm the map at "
                "the end); set minute to see the distribution before the game is decided."
            )
        confidence = {f"share[pos{p.position}]": (p.ci_low, p.ci_high) for p in dist.positions}
        for name, buckets in (
            ("carry_share", dist.carry_share_buckets),
            ("core_share", dist.core_share_buckets),
        ):
            for b in buckets:
                if b.teams:
                    confidence[f"win_rate[{name} {b.label}]"] = (b.ci_low, b.ci_high)
        return ok(
            dist,
            filters=used,
            sample_size=dist.teams,
            period=_period(sample),
            definitions={
                "teams": "Team-matches with inferred positions (sample_size counts these).",
                "share": (
                    "Player's gold / the team's total. Gold is net worth at the end, or "
                    "OpenDota gold_t (gold earned so far) at the minute."
                ),
                "carry_share": "Position 1 share of team gold.",
                "core_share": "Positions 1-3 share of team gold.",
                "difference": "Mean share in won matches minus mean share in lost matches.",
                "share CI": "95% percentile bootstrap of the mean share.",
                "best_carry_share": (
                    f"Bucket with the highest win rate among buckets with at least "
                    f"{economy.MIN_BUCKET_TEAMS} teams; check its interval before calling it "
                    "better."
                ),
            },
            confidence=confidence,
            caveats=caveats,
            source=["opendota"],
            data_freshness=farming.newest_match(sample.match_rows),
        )

    @mcp.tool
    async def get_comeback_stats(
        minute: Annotated[
            int,
            Field(ge=5, le=60, description="Game minute at which the team is behind."),
        ] = 20,
        deficit: Annotated[
            int,
            Field(
                ge=500,
                le=50000,
                description="Gold the team is behind by, at least, at that minute.",
            ),
        ] = 5000,
        filters: Annotated[
            Filters | None,
            Field(
                description=(
                    "Patch and min_sample apply to the league sample; rank_bracket and "
                    "position apply to the ranked-pub hero list from STRATZ."
                )
            ),
        ] = None,
    ) -> ToolResult[economy.ComebackStats]:
        """Chance to win when behind by a gold deficit at a minute, a grid over minutes and
        deficits, factors that go with comebacks (next Roshan, tower, Tormentor) and the
        heroes whose teams come back most. Use for "can we still win 10k down at 30?"."""
        requested = filters or Filters()
        prepared = _league_filters(requested)
        if isinstance(prepared, ToolResult):
            return prepared
        used, caveats = prepared
        sample = await _sample(used)
        if isinstance(sample, ToolResult):
            return sample
        source: list[Source] = ["opendota"]
        pub: list[economy.PubComebackHero] = []
        try:
            pub = await economy.fetch_pub_comebacks(economy.comeback_variables(requested))
            source.append("stratz")
        except StratzError as exc:
            caveats.append(f"STRATZ was unavailable ({exc}); pub_heroes is empty.")
        stats = economy.comeback_stats(used.patch or "", sample, minute, deficit, pub)
        if stats.situations < used.min_sample:
            return _not_enough(
                f"teams behind by {deficit}+ gold at minute {minute}",
                stats.situations,
                used,
                caveats,
                "Lower the deficit, pick a minute between 15 and 35, or lower min_sample.",
                sample,
                source,
            )
        caveats += [
            CORRELATION_CAVEAT,
            "Factors after the minute (next Roshan, tower, Tormentor) are part of how a "
            "comeback happens, not causes chosen in advance.",
            f"Heroes need {economy.MIN_HERO_GAMES}+ behind games to be listed; with this "
            "sample their rates are noisy, so they are ranked by a smoothed rate.",
        ]
        if pub:
            caveats.append(
                "pub_heroes is ranked-pub data from STRATZ (stratz.com) over STRATZ's recent "
                "window. STRATZ does not document how comeBackWon is defined, and it reports "
                "it rounded to two decimals, so many heroes tie."
            )
        confidence = _rate_ci("comeback.win_rate", stats.comeback)
        for f in stats.factors:
            confidence.update(_rate_ci(f"factors[{f.name}].present", f.present))
            confidence.update(_rate_ci(f"factors[{f.name}].absent", f.absent))
        return ok(
            stats,
            filters=used,
            sample_size=stats.situations,
            period=_period(sample),
            definitions={
                "situations": (
                    f"Matches where one team's lead at minute {minute} was <= -{deficit} "
                    "(sample_size counts these)."
                ),
                "lead": LEAD_DEFINITION,
                "comeback.win_rate": "Share of those matches the behind team won.",
                "grid": "The same rate for other minutes and deficits over the same matches.",
                "factors": (
                    "Win rate of the behind team when the factor held (present) vs not "
                    "(absent); p_value from a pooled two-proportion z-test. Matches where the "
                    "factor cannot be judged (no Roshan after the minute, say) are left out."
                ),
                "smoothed_win_rate": (
                    "Empirical-Bayes rate: (wins + prior x "
                    f"{economy.HERO_PRIOR_STRENGTH}) / (games + "
                    f"{economy.HERO_PRIOR_STRENGTH}), prior = overall comeback rate."
                ),
                "comeback_win_share": (
                    "STRATZ heroStats comeBackWon for the hero: read as the share of its "
                    "ranked matches won from behind (STRATZ's own, undocumented, rule)."
                ),
            },
            confidence=confidence,
            caveats=caveats,
            source=source,
            data_freshness=farming.newest_match(sample.match_rows),
        )

    @mcp.tool
    async def get_objective_impact(
        filters: FiltersArg = None,
    ) -> ToolResult[economy.ObjectiveStats]:
        """Win rate of the team that gets first blood, the first tower, first Roshan (Aegis),
        both of the first two Roshans, the first Tormentor, the first barracks, and more
        power / bounty / wisdom runes, with its gold lead at the time and its win rate
        when the game was even. Use for "how much does first Roshan matter?"."""
        prepared = _league_filters(filters or Filters())
        if isinstance(prepared, ToolResult):
            return prepared
        used, caveats = prepared
        sample = await _sample(used)
        if isinstance(sample, ToolResult):
            return sample
        stats = economy.objective_stats(used.patch or "", sample)
        if stats.matches < used.min_sample:
            return _not_enough(
                "matches", stats.matches, used, caveats, "Use an older patch.", sample
            )
        caveats += [
            CORRELATION_CAVEAT,
            "when_even is closer to the objective's own effect: it keeps only matches where "
            f"the gold lead at the event was within +/-{economy.EVEN_LEAD}, but those games "
            "are fewer and the interval is wider.",
            "Rune counts are over the whole match, so a team that is winning also controls "
            "more runes; read them as a symptom more than a cause.",
            "First blood is the earliest hero kill by an enemy hero in kills_log; deaths to "
            "creeps or towers are not counted.",
        ]
        confidence: dict[str, tuple[float, float]] = {}
        for o in stats.objectives:
            confidence.update(_rate_ci(f"{o.objective}.win_rate", o.taker))
            if o.when_even is not None:
                confidence.update(_rate_ci(f"{o.objective}.when_even", o.when_even))
        return ok(
            stats,
            filters=used,
            sample_size=stats.matches,
            period=_period(sample),
            definitions={
                "decided": "Matches where one team got the objective (sample_size: all matches).",
                "taker.win_rate": "Share of those matches the team that got it won.",
                "lift": "taker.win_rate - 0.5.",
                "p_value": "Two-sided normal-approximation test of taker.win_rate = 0.5.",
                "median_lead_at_event": "Taker's median lead at the event minute. "
                + LEAD_DEFINITION,
                "when_even": (
                    f"Taker win rate when its lead at the event was within +/-"
                    f"{economy.EVEN_LEAD} gold."
                ),
                "power runes": (
                    "Double damage, haste, illusion, invisibility, regeneration, arcane and "
                    "shield (Dota rune ids 0-4, 6, 9 in OpenDota's runes counts)."
                ),
                "objectives": (
                    "Towers, barracks, Roshan and Tormentor from OpenDota objectives; a "
                    "building counts for the side that did not own it, also when creeps "
                    "finished it."
                ),
            },
            confidence=confidence,
            caveats=caveats,
            source=["opendota"],
            data_freshness=farming.newest_match(sample.match_rows),
        )
