"""Player tools: profile, stats, hero pool, weaknesses, trends, peers and comparisons."""

from collections.abc import Awaitable
from typing import Annotated

from fastmcp import FastMCP
from pydantic import BeforeValidator, Field

from dota_insights.analytics import players as analytics
from dota_insights.analytics.players import (
    InsufficientData,
    MatchQuery,
    PlayerInputError,
    PlayerQueryError,
    PlayerUnavailable,
    Report,
)
from dota_insights.mcp.envelope import ToolResult, error, insufficient_data, ok
from dota_insights.models.filters import Filters, GameMode, Position
from dota_insights.reference import resolve_hero
from dota_insights.sources.opendota import OpenDotaError, OpenDotaRateLimitedError

PLAYER_HINT = (
    "Pass the 32-bit account ID (as on OpenDota/Dotabuff/STRATZ), a SteamID64, or a "
    "steamcommunity.com/profiles/, opendota.com/players/, stratz.com/players/ or "
    "dotabuff.com/players/ URL."
)

Player = Annotated[
    str,
    Field(
        description=(
            "The player: 32-bit Dota account ID (e.g. 56939869), SteamID64 "
            "(7656119...), or a Steam /profiles/, OpenDota, STRATZ or Dotabuff profile URL. "
            "Only public profiles work. Names are not searched."
        )
    ),
]
Matches = Annotated[
    int,
    Field(
        ge=1,
        le=analytics.MAX_MATCHES,
        description="How many of the player's most recent matches to analyse (1-500).",
    ),
]
Days = Annotated[
    int | None,
    Field(
        ge=1,
        le=3650,
        description="Only matches from the last N days. Combined with `matches` as a cap.",
    ),
]
Hero = Annotated[
    str | None,
    Field(description='Only matches on this hero; names and nicknames work ("PA", "am").'),
]
PositionArg = Annotated[
    Position | None,
    BeforeValidator(Position.parse),
    Field(
        description=(
            "Only matches at this position: 1 carry, 2 mid, 3 offlane, 4 soft support, "
            "5 hard support. Needs STRATZ; unparsed matches use a lane/role inference."
        )
    ),
]
GameModeArg = Annotated[
    GameMode | None,
    Field(description="Only matches in this mode, e.g. ranked_all_pick or turbo. Omit for all."),
]


MinSample = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Fewest matches needed for an answer; below it the tool returns "
            '"insufficient_data". Prefer widening matches/days over lowering it.'
        ),
    ),
]


def _wrap(report: Report, filters: Filters | None) -> ToolResult:
    return ok(
        report.result,
        filters=filters,
        sample_size=report.sample_size,
        period=report.period,
        definitions=report.definitions,
        confidence=report.confidence,
        caveats=report.caveats,
        source=report.sources,
        data_freshness=report.data_freshness,
    )


async def _run(work: Awaitable[Report], filters: Filters | None = None) -> ToolResult:
    """Await an analytics report and turn it, or its failure, into an envelope."""
    try:
        report = await work
    except PlayerInputError as exc:
        return error(str(exc), hint=PLAYER_HINT)
    except PlayerUnavailable as exc:
        return error(str(exc), source=["opendota"])
    except PlayerQueryError as exc:
        return error(str(exc), filters=filters)
    except InsufficientData as exc:
        return insufficient_data(exc.reason, exc.hint, filters=filters, sample_size=exc.sample_size)
    except OpenDotaRateLimitedError:
        return error("OpenDota rate limit reached.", hint="Retry in a minute.", source=["opendota"])
    except OpenDotaError as exc:
        return error(f"OpenDota request failed: {exc}", source=["opendota"])
    return _wrap(report, filters)


def _query(
    matches: int,
    days: int | None,
    hero: str | None,
    position: Position | None,
    game_mode: GameMode | None,
) -> MatchQuery | ToolResult:
    hero_id = None
    if hero:
        resolution = resolve_hero(hero)
        if resolution.best is None:
            names = ", ".join(c.record.display_name for c in resolution.candidates)
            return error(
                f"Hero {hero!r} is {'ambiguous' if names else 'unknown'}"
                + (f": {names}." if names else "."),
                hint="Pass one of the listed hero names.",
            )
        hero_id = resolution.best.id
    return MatchQuery(
        matches=matches, days=days, hero_id=hero_id, position=position, game_mode=game_mode
    )


def _account(player: str) -> int | ToolResult:
    try:
        return analytics.parse_player(player)
    except PlayerInputError as exc:
        return error(str(exc), hint=PLAYER_HINT)


def _filters(query: MatchQuery, min_sample: int) -> Filters:
    return Filters(position=query.position, game_mode=query.game_mode, min_sample=min_sample)


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_player_profile(player: Player) -> ToolResult:
        """A player's medal, leaderboard rank, total matches, overall win rate and a summary
        of their last 20 matches. Start here before deeper player questions."""
        account = _account(player)
        if isinstance(account, ToolResult):
            return account
        return await _run(analytics.profile_report(account))

    @mcp.tool
    async def get_player_stats(
        player: Player,
        matches: Matches = 100,
        days: Days = None,
        hero: Hero = None,
        position: PositionArg = None,
        game_mode: GameModeArg = None,
        min_sample: MinSample = 10,
    ) -> ToolResult:
        """Average KDA, kills, deaths, assists, GPM, XPM, last hits, hero/tower damage and
        win rate over a player's recent matches, with 95% intervals. Optionally only one
        hero, position or game mode."""
        account = _account(player)
        if isinstance(account, ToolResult):
            return account
        query = _query(matches, days, hero, position, game_mode)
        if isinstance(query, ToolResult):
            return query
        filters = _filters(query, min_sample)
        return await _run(analytics.stats_report(account, query, min_sample), filters)

    @mcp.tool
    async def get_player_hero_pool(
        player: Player,
        matches: Matches = 100,
        days: Days = None,
        game_mode: GameModeArg = None,
        min_hero_matches: Annotated[
            int,
            Field(ge=1, description="Fewest matches on a hero before it is judged."),
        ] = 5,
        min_sample: MinSample = 10,
    ) -> ToolResult:
        """Matches and win rate per hero, compared with that hero's win rate and OpenDota
        benchmarks (GPM, XPM, last hits, damage percentiles) at the player's medal, and
        the heroes where the player over- or under-performs."""
        account = _account(player)
        if isinstance(account, ToolResult):
            return account
        query = MatchQuery(matches=matches, days=days, game_mode=game_mode)
        filters = _filters(query, min_sample)
        return await _run(
            analytics.hero_pool_report(account, query, min_sample, min_hero_matches), filters
        )

    @mcp.tool
    async def find_player_weaknesses(
        player: Player,
        matches: Matches = 100,
        days: Days = None,
        hero: Hero = None,
        position: PositionArg = None,
        game_mode: GameModeArg = None,
        min_sample: MinSample = 20,
    ) -> ToolResult:
        """Where a player falls short: per-minute stats below their medal's percentiles,
        the game phase (10/20/30 min net worth and XP) where losses fall behind wins, and
        traits more common in lost matches. Every finding carries sample sizes and caveats."""
        account = _account(player)
        if isinstance(account, ToolResult):
            return account
        query = _query(matches, days, hero, position, game_mode)
        if isinstance(query, ToolResult):
            return query
        filters = _filters(query, min_sample)
        return await _run(analytics.weaknesses_report(account, query, min_sample), filters)

    @mcp.tool
    async def get_player_trends(
        player: Player,
        matches: Matches = 100,
        days: Days = None,
        hero: Hero = None,
        game_mode: GameModeArg = None,
        bucket_size: Annotated[
            int, Field(ge=2, le=100, description="Matches per time bucket, oldest first.")
        ] = 10,
        min_sample: MinSample = 10,
    ) -> ToolResult:
        """How a player's win rate, KDA, GPM, XPM, last hits and damage change over their
        recent matches, plus longest and current win/loss streaks."""
        account = _account(player)
        if isinstance(account, ToolResult):
            return account
        query = _query(matches, days, hero, None, game_mode)
        if isinstance(query, ToolResult):
            return query
        filters = _filters(query, min_sample)
        return await _run(analytics.trends_report(account, query, min_sample, bucket_size), filters)

    @mcp.tool
    async def get_player_peers(
        player: Player,
        matches: Matches = 200,
        days: Days = None,
        game_mode: GameModeArg = None,
        min_games: Annotated[
            int, Field(ge=1, description="Fewest shared matches before a peer is listed.")
        ] = 5,
        top: Annotated[int, Field(ge=1, le=50, description="Peers per list.")] = 10,
    ) -> ToolResult:
        """The player's most frequent teammates and opponents over recent matches, with the
        player's win rate with or against each."""
        account = _account(player)
        if isinstance(account, ToolResult):
            return account
        query = MatchQuery(matches=matches, days=days, game_mode=game_mode)
        filters = _filters(query, 1)
        return await _run(analytics.peers_report(account, query, min_games, top), filters)

    @mcp.tool
    async def compare_players(
        player_a: Player,
        player_b: Player,
        matches: Matches = 100,
        days: Days = None,
        game_mode: GameModeArg = None,
        min_hero_matches: Annotated[
            int,
            Field(ge=1, description="Fewest matches each player needs on a shared hero."),
        ] = 3,
        min_sample: MinSample = 10,
    ) -> ToolResult:
        """Two players side by side: overall win rate and averages, then the heroes both play
        and each position they play."""
        account_a, account_b = _account(player_a), _account(player_b)
        for account in (account_a, account_b):
            if isinstance(account, ToolResult):
                return account
        query = MatchQuery(matches=matches, days=days, game_mode=game_mode)
        filters = _filters(query, min_sample)
        return await _run(
            analytics.compare_report(account_a, account_b, query, min_sample, min_hero_matches),
            filters,
        )
