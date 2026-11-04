"""Pro scene tools: pro matches, teams, head-to-head, tournaments, pro players, builds, live."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from dota_insights import privacy
from dota_insights.analytics import pro
from dota_insights.cache import TTL_LIVE
from dota_insights.mcp import args
from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.mcp.errors import source_error
from dota_insights.models import Filters
from dota_insights.reference import resolve_hero
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

STRATZ_CREDIT = "Team, player and build data from STRATZ (https://stratz.com)."
SOURCE_ERRORS = (OpenDotaError, StratzError, pro.ExplorerError)

TeamArg = Annotated[
    str,
    Field(
        description=(
            'Team name, tag or OpenDota team id, e.g. "Team Spirit", "OG", "7119388". '
            "Matched fuzzily; if several teams match, the result lists them."
        )
    ),
]
LeagueArg = Annotated[
    str,
    Field(
        description=(
            'Tournament (league) name or OpenDota league id, e.g. "The International 2025", '
            '"EPL Masters", "18324". Matched fuzzily against professional and premium leagues.'
        )
    ),
]


def _period(matches) -> Period | None:
    span = pro.date_range(matches)
    return Period(from_date=span[0], to_date=span[1]) if span else None


def _source_error(exc: Exception, source: list) -> ToolResult:
    if isinstance(exc, OpenDotaError | StratzError):
        return source_error(exc)
    return source_error(exc, source=source)


def _not_found(kind: str, lookup: pro.Lookup, label) -> ToolResult:
    if lookup.candidates:
        names = "; ".join(label(c) for c in lookup.candidates)
        return error(
            f"{kind} {lookup.query!r} is ambiguous. Candidates: {names}.",
            hint="Repeat the call with one of the names or ids above.",
            source=["opendota"],
        )
    return error(
        f"No {kind.lower()} matches {lookup.query!r}.",
        hint="Check the spelling, or pass the OpenDota id.",
        source=["opendota"],
    )


def _namesake_caveat(kind: str, lookup: pro.Lookup, label) -> list[str]:
    if not lookup.namesakes:
        return []
    others = "; ".join(label(r) for r in lookup.namesakes)
    return [
        f"Several {kind}s share the name {lookup.query!r}; using {label(lookup.best)}. "
        f"Others: {others}. Pass an id to pick another."
    ]


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_pro_matches(
        team: Annotated[
            str | None, Field(description="Only matches of this team (name, tag or id).")
        ] = None,
        league: Annotated[
            str | None, Field(description="Only matches of this tournament (name or id).")
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=50, description="How many matches, newest first.")
        ] = 20,
        include_drafts: Annotated[
            bool, Field(description="Add picks and bans per match (costs extra source calls).")
        ] = True,
    ) -> ToolResult[list[dict]]:
        """Recent professional (league) matches: teams, league, winner, score, duration, draft.

        Without filters returns the newest league matches of any tier. Use get_team_stats or
        get_tournament_stats for aggregates instead of summarizing these rows yourself.
        """
        caveats: list[str] = []
        source: list = ["opendota"]
        try:
            team_record = league_record = None
            if team:
                lookup = await pro.resolve_team(team)
                if lookup.best is None:
                    return _not_found("Team", lookup, pro.team_label)
                team_record = lookup.best
                caveats += _namesake_caveat("team", lookup, pro.team_label)
            if league:
                lookup = await pro.resolve_league(league)
                if lookup.best is None:
                    return _not_found("League", lookup, pro.league_label)
                league_record = lookup.best
                caveats += _namesake_caveat("league", lookup, pro.league_label)

            if team_record:
                source.append("stratz")
                since = int((datetime.now(UTC) - timedelta(days=365)).timestamp())
                _, matches = await pro.fetch_team_matches(team_record["team_id"], since)
                if league_record:
                    matches = [m for m in matches if m.league_id == league_record["leagueid"]]
                caveats.append(f"Team matches from the last 365 days. {STRATZ_CREDIT}")
            elif league_record:
                matches = await pro.fetch_league_matches(league_record["leagueid"])
            else:
                matches = [pro.match_from_opendota(r) for r in await pro.fetch_recent_pro_matches()]
                caveats.append(
                    "OpenDota's /proMatches covers the newest 100 league matches of all tiers, "
                    "including small leagues; unnamed teams appear as null."
                )
            matches = sorted(matches, key=lambda m: -m.start_time)[:limit]
            await pro.fill_names(matches)
            if include_drafts:
                missing = tuple(m.match_id for m in matches if m.picks_bans is None)
                if missing:
                    try:
                        drafts = await pro.fetch_drafts(missing)
                    except StratzError as exc:
                        caveats.append(f"Drafts unavailable: {exc}")
                    else:
                        if "stratz" not in source:
                            source.append("stratz")
                            caveats.append(STRATZ_CREDIT)
                        for m in matches:
                            if m.match_id in drafts:
                                m.picks_bans = drafts[m.match_id]
        except SOURCE_ERRORS as exc:
            return _source_error(exc, source)

        if not matches:
            return insufficient_data(
                "No pro matches found for these filters.",
                hint="Drop the league or team filter, or check the names.",
                source=source,
            )
        return ok(
            [pro.match_summary(m, include_drafts) for m in matches],
            sample_size=len(matches),
            period=_period(matches),
            definitions={
                "winner": "Team name of the winning side (side name if the team is unknown).",
                "score": "Kills of that side.",
                "draft": "Picks and bans in order; first_pick is the side that picked first.",
            },
            caveats=caveats,
            source=source,
            data_freshness=pro.latest_end(matches),
        )

    @mcp.tool
    async def get_team_stats(
        team: TeamArg,
        days: Annotated[
            int, Field(ge=7, le=365, description="Look-back window in days (period).")
        ] = 90,
        min_matches: Annotated[
            int, Field(ge=1, description="Fewest matches needed to report rates.")
        ] = 10,
    ) -> ToolResult[dict]:
        """A pro team's win rate, average duration, hero pool and draft tendencies.

        Draft tendencies: how often the team takes first pick, the heroes it bans most and the
        heroes opponents ban against it.
        """
        try:
            lookup = await pro.resolve_team(team)
            if lookup.best is None:
                return _not_found("Team", lookup, pro.team_label)
            record = lookup.best
            since = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
            info, matches = await pro.fetch_team_matches(record["team_id"], since)
        except SOURCE_ERRORS as exc:
            return _source_error(exc, ["opendota", "stratz"])

        filters = Filters(min_sample=min_matches)
        stats = pro.team_stats(record["team_id"], matches)
        caveats = _namesake_caveat("team", lookup, pro.team_label) + [
            f"All of the team's matches on STRATZ in the last {days} days (league and "
            f"tournament lobbies), up to {pro.MAX_TEAM_MATCHES}.",
            "Hero win rates per hero rest on few games; treat them as descriptive.",
            STRATZ_CREDIT,
        ]
        source: list = ["opendota", "stratz"]
        if stats["matches"] < min_matches:
            return insufficient_data(
                f"{pro.team_label(record)} played {stats['matches']} matches in {days} days.",
                hint="Increase days (up to 365) or lower min_matches.",
                filters=filters,
                sample_size=stats["matches"],
                caveats=caveats,
                source=source,
            )
        name = (info or {}).get("name") or record.get("name")
        result = {"team": name, "team_id": record["team_id"], "days": days, **stats}
        confidence = {"win_rate": stats["win_rate_ci"]}
        if stats["avg_duration_ci"]:
            confidence["avg_duration_min"] = stats["avg_duration_ci"]
        return ok(
            result,
            filters=filters,
            sample_size=stats["matches"],
            period=_period(matches),
            definitions={
                "win_rate": "Wins / matches.",
                "avg_duration_min": "Mean match length in minutes (CI: bootstrap of the mean).",
                "hero_pool_size": "Distinct heroes the team picked.",
                "first_pick_share": "Share of drafted matches where the team made the first pick.",
                "most_banned_by_team": "Heroes the team banned, by count.",
                "most_banned_against_team": "Heroes opponents banned against the team.",
            },
            confidence=confidence,
            caveats=caveats,
            source=source,
            data_freshness=pro.latest_end(matches),
        )

    @mcp.tool
    async def get_head_to_head(
        team_a: TeamArg,
        team_b: TeamArg,
        limit: Annotated[
            int, Field(ge=1, le=30, description="How many of the newest meetings to list.")
        ] = 10,
        min_meetings: Annotated[
            int, Field(ge=1, description="Fewest meetings needed to report a record.")
        ] = 1,
    ) -> ToolResult[dict]:
        """Past league meetings between two teams: overall record, by league, and drafts."""
        try:
            looks = [await pro.resolve_team(t) for t in (team_a, team_b)]
            for lookup in looks:
                if lookup.best is None:
                    return _not_found("Team", lookup, pro.team_label)
            a, b = (lookup.best for lookup in looks)
            if a["team_id"] == b["team_id"]:
                return error("Both names resolve to the same team.", source=["opendota"])
            matches = await pro.fetch_head_to_head(a["team_id"], b["team_id"])
        except SOURCE_ERRORS as exc:
            return _source_error(exc, ["opendota"])

        filters = Filters(min_sample=min_meetings)
        caveats = [c for lookup in looks for c in _namesake_caveat("team", lookup, pro.team_label)]
        caveats += [
            "League matches only (OpenDota explorer), newest 100 meetings. Rosters change: "
            "old meetings may involve different players.",
        ]
        stats = pro.head_to_head(a["team_id"], b["team_id"], matches, limit)
        if stats["meetings"] < min_meetings:
            return insufficient_data(
                f"{pro.team_label(a)} and {pro.team_label(b)} have "
                f"{stats['meetings']} recorded league meetings.",
                hint="Check the team names; lower min_meetings.",
                filters=filters,
                sample_size=stats["meetings"],
                caveats=caveats,
                source=["opendota"],
            )
        result = {
            "team_a": {"team": a.get("name"), "team_id": a["team_id"]},
            "team_b": {"team": b.get("name"), "team_id": b["team_id"]},
            **stats,
        }
        return ok(
            result,
            filters=filters,
            sample_size=stats["meetings"],
            period=_period(matches),
            definitions={
                "team_a_win_rate": "Team A wins / meetings (games, not series).",
                "by_league": "Meetings and team A wins per league.",
            },
            confidence={"team_a_win_rate": stats["team_a_win_rate_ci"]},
            caveats=caveats,
            source=["opendota"],
            data_freshness=pro.latest_end(matches),
        )

    @mcp.tool
    async def get_tournament_stats(
        league: LeagueArg,
        min_picks: Annotated[
            int, Field(ge=1, description="Fewest picks for a hero to appear in win-rate lists.")
        ] = 5,
        min_matches: Annotated[
            int, Field(ge=1, description="Fewest matches needed to report tournament stats.")
        ] = 10,
    ) -> ToolResult[dict]:
        """Hero picks, bans, contest rate and win rates in a tournament, with match records.

        Each hero's win rate in the event is compared with its public-match win rate
        (OpenDota, last 7 days, all ranks). Records: longest, shortest and bloodiest match.
        """
        try:
            lookup = await pro.resolve_league(league)
            if lookup.best is None:
                return _not_found("League", lookup, pro.league_label)
            record = lookup.best
            matches = await pro.fetch_league_matches(record["leagueid"])
            public = await pro.fetch_public_hero_stats()
        except SOURCE_ERRORS as exc:
            return _source_error(exc, ["opendota"])

        filters = Filters(min_sample=min_matches)
        caveats = _namesake_caveat("league", lookup, pro.league_label) + [
            "Pro and public win rates are not directly comparable: pro picks are drafted "
            "against specific opponents, and public rates are from the last 7 days (current "
            "patch) whereas the event may have been on an older patch.",
            "Per-hero win rates in one event rest on few games; check win_rate_ci.",
        ]
        stats = pro.tournament_stats(matches, public, min_picks)
        if len(matches) >= pro.EXPLORER_LIMIT:
            caveats.append(f"Only the newest {pro.EXPLORER_LIMIT} matches of the league are used.")
        name = (record.get("name") or "").strip() or None
        if stats["matches"] < min_matches:
            return insufficient_data(
                f"{pro.league_label(record)} has {stats['matches']} finished matches on OpenDota.",
                hint="Check the league name, or lower min_matches.",
                filters=filters,
                sample_size=stats["matches"],
                caveats=caveats,
                source=["opendota"],
            )
        return ok(
            {"league": name, "league_id": record["leagueid"], "tier": record.get("tier"), **stats},
            filters=filters,
            sample_size=stats["matches"],
            period=_period(matches),
            definitions={
                "contest_rate": "(picks + bans) / matches with a draft.",
                "pick_rate": "Picks / matches with a draft.",
                "ban_rate": "Bans / matches with a draft.",
                "win_rate": "Wins / picks of the hero in the event.",
                "public_win_rate": "OpenDota pub_win / pub_pick (all ranks, last 7 days).",
                "vs_public": "win_rate - public_win_rate.",
                "most_kills": "Highest combined kill score of both teams in one match.",
            },
            confidence={"radiant_win_rate": stats["radiant_win_rate_ci"]},
            caveats=caveats,
            source=["opendota"],
            data_freshness=pro.latest_end(matches),
        )

    @mcp.tool
    async def get_pro_player(
        player: Annotated[
            str,
            Field(description='Pro name or Steam account id, e.g. "Yatoro", "321580662".'),
        ],
        matches: Annotated[
            int, Field(ge=10, le=100, description="How many newest tournament matches to use.")
        ] = 100,
        min_matches: Annotated[
            int, Field(ge=1, description="Fewest matches needed to report rates.")
        ] = 10,
    ) -> ToolResult[dict]:
        """A pro player's team, signature heroes and results by position in league matches."""
        try:
            lookup = await pro.resolve_pro_player(player)
            if lookup.best is None:
                return _not_found("Player", lookup, pro.player_label)
            record = lookup.best
            if await privacy.is_opted_out(record["account_id"]):
                return error(
                    privacy.OPTED_OUT_MESSAGE.format(account=record["account_id"]),
                    hint=privacy.OPTED_OUT_HINT,
                )
            data = await pro.fetch_player_matches(record["account_id"], matches)
        except SOURCE_ERRORS as exc:
            return _source_error(exc, ["opendota", "stratz"])

        if not data:
            return error(
                f"STRATZ has no profile for account {record['account_id']}.",
                source=["stratz"],
            )
        filters = Filters(min_sample=min_matches)
        source: list = ["opendota", "stratz"]
        stats = pro.pro_player_stats(data)
        caveats = _namesake_caveat("player", lookup, pro.player_label) + [
            "League matches in tournament lobbies only; public games are excluded.",
            "Position is STRATZ's own role detection.",
            STRATZ_CREDIT,
        ]
        if stats["matches"] < min_matches:
            return insufficient_data(
                f"{pro.player_label(record)} has {stats['matches']} league matches on STRATZ.",
                hint="Lower min_matches, or check the player name.",
                filters=filters,
                sample_size=stats["matches"],
                caveats=caveats,
                source=source,
            )
        pro_account = (data.get("steamAccount") or {}).get("proSteamAccount") or {}
        team = ((data.get("team") or {}).get("team")) or {}
        result = {
            "player": pro_account.get("name") or record.get("name"),
            "account_id": record["account_id"],
            "real_name": pro_account.get("realName"),
            "team": team.get("name") or record.get("team_name"),
            "team_id": team.get("id") or record.get("team_id"),
            "listed_role": pro_account.get("position"),
            **stats,
        }
        times = [m["startDateTime"] for m in data.get("matches") or []]
        return ok(
            result,
            filters=filters,
            sample_size=stats["matches"],
            period=Period(from_date=pro.utc(min(times)).date(), to_date=pro.utc(max(times)).date()),
            definitions={
                "share": "Games on the hero or position / all games used.",
                "kda": "(kills + assists) / max(deaths, 1), summed over games.",
                "avg_gpm": "Mean gold per minute.",
            },
            confidence={"win_rate": stats["win_rate_ci"]},
            caveats=caveats,
            source=source,
            data_freshness=pro.utc(max(times)),
        )

    @mcp.tool
    async def get_pro_builds(
        hero: Annotated[str, Field(description=args.HERO)],
        matches: Annotated[
            int,
            Field(ge=10, le=60, description="How many of the hero's newest pro matches to use."),
        ] = 30,
        min_matches: Annotated[
            int, Field(ge=1, description="Fewest pro matches needed to report builds.")
        ] = 10,
    ) -> ToolResult[dict]:
        """Items and skill builds pros use on a hero, next to public-match builds.

        Items: share of games where pros bought each item (and when) against the public share.
        Skills: the most common first ten skill points, and the order in which abilities are
        maxed by pros and in public games.
        """
        resolution = resolve_hero(hero)
        if resolution.best is None:
            names = ", ".join(c.record.display_name for c in resolution.candidates)
            return error(
                f"Hero {hero!r} is {'ambiguous' if names else 'unknown'}."
                + (f" Candidates: {names}." if names else ""),
                hint="Use the hero's full name.",
            )
        hero_record = resolution.best
        source: list = ["opendota", "stratz"]
        try:
            listed = await pro.fetch_hero_pro_matches(hero_record.id)
            pairs = tuple(
                (int(r["match_id"]), int(r["account_id"]))
                for r in sorted(listed, key=lambda r: -r["start_time"])[:matches]
                if r.get("account_id")
            )
            rows = await pro.fetch_hero_builds(pairs)
            public = await pro.fetch_public_builds(hero_record.id)
        except SOURCE_ERRORS as exc:
            return _source_error(exc, source)

        filters = Filters(min_sample=min_matches)
        rows = [r for r in rows if r.get("heroId") == hero_record.id]
        caveats = [
            "Item win rates are correlational: items bought in won games look better.",
            "Public numbers are STRATZ aggregates over its default recent window and all "
            "ranks; pro numbers are from the newest league matches with the hero.",
            "Maxed-at for pros counts skill points (≈ hero level); for public games it is "
            "STRATZ's hero level when the ability was maxed.",
            STRATZ_CREDIT,
        ]
        if len(rows) < min_matches:
            return insufficient_data(
                f"Only {len(rows)} recent pro matches with {hero_record.display_name} "
                "have build data.",
                hint="Raise matches (up to 60) or lower min_matches.",
                filters=filters,
                sample_size=len(rows),
                caveats=caveats,
                source=source,
            )
        stats = pro.pro_builds(rows, public)
        positions = {}
        for r in rows:
            pos = pro.POSITIONS.get(r.get("position"))
            positions[pos] = positions.get(pos, 0) + 1
        times = [r["start_time"] for r in rows if r.get("start_time")]
        return ok(
            {
                "hero": hero_record.display_name,
                "pro_positions": [
                    {"position": p, "games": n}
                    for p, n in sorted(positions.items(), key=lambda kv: -kv[1])
                ],
                **stats,
            },
            filters=filters,
            sample_size=len(rows),
            period=Period(from_date=pro.utc(min(times)).date(), to_date=pro.utc(max(times)).date())
            if times
            else None,
            definitions={
                "pro_share": "Pro games where the item was bought / pro games used.",
                "pro_rank": "Rank of the item by pro games bought (1 = most bought).",
                "public_purchases": "STRATZ count of first purchases in public games. Not "
                "unique matches and not on the same window as public_games, so no share.",
                "public_rank": "Rank of the item by public_purchases (1 = most bought).",
                "pro_median_min": "Median minute of the first purchase in pro games.",
                "public_typical_min": "Median minute of the first purchase in public games "
                "(mean for boots, which STRATZ reports separately).",
                "public_games": "STRATZ public matches on the hero (heroStats.stats).",
                "pro_median_maxed_at": "Median skill-point number at which pros maxed the "
                "ability (the point count most pros reach).",
                "public_median_maxed_level": "Median hero level at which public players maxed it.",
            },
            caveats=caveats,
            source=source,
            data_freshness=pro.utc(max(times)) if times else None,
        )

    @mcp.tool
    async def get_live_matches(
        limit: Annotated[int, Field(ge=1, le=30, description="How many live games.")] = 10,
        pro_only: Annotated[
            bool,
            Field(description="Only league games or games with registered pro players."),
        ] = True,
    ) -> ToolResult[list[dict]]:
        """Top live games now: teams, players, game time, score and net-worth lead."""
        try:
            games = await pro.fetch_live()
            leagues = await pro.league_names()
        except SOURCE_ERRORS as exc:
            return _source_error(exc, ["opendota"])
        live = pro.live_games(games, leagues, limit, pro_only)
        if not live:
            return insufficient_data(
                "No live games match right now.",
                hint="Set pro_only to false to include top public games.",
                source=["opendota"],
            )
        delays = sorted({g["delay_s"] for g in live if g.get("delay_s") is not None})
        updates = [g["last_update_time"] for g in games if g.get("last_update_time")]
        delay_text = (
            f"{', '.join(str(d) for d in delays)} s" if delays else "an unknown number of seconds"
        )
        return ok(
            live,
            sample_size=len(live),
            definitions={
                "radiant_networth_lead": "Radiant net worth minus Dire net worth (negative: "
                "Dire leads).",
                "delay_s": "Broadcast delay of the game data in seconds.",
                "game_time": "In-game clock at the last update (mm:ss).",
            },
            caveats=[
                f"Live data is delayed: the game feed lags by {delay_text}, and OpenDota's "
                "list is refreshed about once a minute (this server caches it for "
                f"{TTL_LIVE} s). Scores may be a minute or more behind the stream.",
                "Hero can be null during the draft.",
            ],
            source=["opendota"],
            data_freshness=pro.utc(max(updates)) if updates else None,
        )
