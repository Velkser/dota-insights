"""Player analytics: profile, averages, hero pool, weaknesses, trends, peers and comparisons.

OpenDota ``/players/*`` is the primary source: profile, win/loss totals, the match list, peers
and hero benchmarks. STRATZ supplements it with each match's position and, for parsed matches,
net worth and XP by minute. When STRATZ is unavailable those parts are left out with a caveat.

Every public ``*_report`` function returns a `Report` (the numbers plus provenance) or raises
`InsufficientData`, `PlayerUnavailable` or `PlayerQueryError`; the tool layer turns those into
result envelopes.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from statistics import fmean, median, stdev
from typing import Any

from dota_insights.analytics.stats import (
    benjamini_hochberg,
    bootstrap_ci,
    two_proportion_ztest,
    wilson_interval,
)
from dota_insights.cache import TTL_AGGREGATE, cached
from dota_insights.mcp.envelope import Period, Source
from dota_insights.models.filters import GameMode, Position, RankBracket
from dota_insights.reference import hero_by_id
from dota_insights.sources import get_opendota, get_stratz
from dota_insights.sources.opendota import OpenDotaError, OpenDotaNotFoundError
from dota_insights.sources.stratz import StratzError

# A player's match list changes with every game, so cache it briefly.
TTL_PLAYER = 15 * 60

STEAM64_BASE = 76561197960265728
ANONYMOUS_ACCOUNT_ID = 4294967295
MAX_MATCHES = 500
STRATZ_PAGE = 100
# Hero benchmarks cost one OpenDota call per hero, so only the most played heroes get them.
MAX_BENCHMARK_HEROES = 5
PHASE_MINUTES = (10, 20, 30)
MIN_PHASE_GROUP = 5
BOOTSTRAP_RESAMPLES = 1000

# OpenDota ``game_mode`` and ``lobby_type`` ids for each GameMode filter.
GAME_MODE_PARAMS: dict[GameMode, tuple[int, int | None]] = {
    GameMode.RANKED_ALL_PICK: (22, 7),
    GameMode.ALL_PICK: (22, 0),
    GameMode.TURBO: (23, None),
    GameMode.CAPTAINS_MODE: (2, None),
    GameMode.SINGLE_DRAFT: (4, None),
    GameMode.RANDOM_DRAFT: (3, None),
    GameMode.ALL_RANDOM: (5, None),
    GameMode.ABILITY_DRAFT: (18, None),
}

MATCH_FIELDS = (
    "hero_id",
    "start_time",
    "duration",
    "game_mode",
    "lobby_type",
    "kills",
    "deaths",
    "assists",
    "gold_per_min",
    "xp_per_min",
    "last_hits",
    "denies",
    "hero_damage",
    "tower_damage",
    "hero_healing",
    "party_size",
    "version",
)

STRATZ_DETAILS_QUERY = """
query PlayerMatchDetails($id: Long!, $matchIds: [Long]!, $take: Int!) {
  player(steamAccountId: $id) {
    steamAccountId
    matches(request: {matchIds: $matchIds, take: $take}) {
      id
      parsedDateTime
      players(steamAccountId: $id) {
        position
        lane
        role
        stats { networthPerMinute experiencePerMinute }
      }
    }
  }
}
"""

MEDALS = [b.value.capitalize() for b in RankBracket]

COMMON_CAVEATS = [
    "Only matches OpenDota has for this account are counted; matches played while "
    '"Expose Public Match Data" was off are missing.',
]
STRATZ_CAVEAT = "Positions and per-minute data come from STRATZ (https://stratz.com)."


# Errors


class PlayerInputError(ValueError):
    """The player argument is not an account ID, SteamID or supported profile URL."""


class PlayerUnavailable(Exception):
    """The player is unknown or keeps match data private."""


class PlayerQueryError(Exception):
    """The query cannot be answered as asked (for example a filter needs an absent source)."""


class InsufficientData(Exception):
    def __init__(self, reason: str, hint: str, sample_size: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.sample_size = sample_size


# Player references


_ID_URLS = [
    re.compile(r"steamcommunity\.com/profiles/(\d+)", re.I),
    re.compile(r"opendota\.com/players/(\d+)", re.I),
    re.compile(r"stratz\.com/players/(\d+)", re.I),
    re.compile(r"dotabuff\.com/players/(\d+)", re.I),
]
_VANITY_URL = re.compile(r"steamcommunity\.com/id/([^/?#\s]+)", re.I)
_LEGACY_ID = re.compile(r"STEAM_[0-5]:([01]):(\d+)", re.I)
_STEAM3_ID = re.compile(r"\[?U:1:(\d+)\]?", re.I)


def _account_from_number(number: int) -> int:
    if number >= 2**32:
        universe = number >> 56
        account_type = (number >> 52) & 0xF
        if universe != 1 or account_type != 1:
            raise PlayerInputError(
                f"{number} is not the SteamID64 of an individual public account."
            )
        number = number & 0xFFFFFFFF
    if number == 0 or number == ANONYMOUS_ACCOUNT_ID:
        raise PlayerInputError(
            f"{number} is not a real account: Dota uses it for anonymous players."
        )
    return number


def parse_player(value: str | int) -> int:
    """32-bit account ID from an account ID, SteamID64, STEAM_X:Y:Z, [U:1:N] or profile URL.

    Supported URLs: steamcommunity.com/profiles/<SteamID64>, opendota.com/players/<id>,
    stratz.com/players/<id> and dotabuff.com/players/<id>. Custom Steam URLs
    (steamcommunity.com/id/<name>) need the Steam Web API, which this server has no key for.
    """
    text = str(value).strip()
    if not text:
        raise PlayerInputError("No player given.")
    if match := _VANITY_URL.search(text):
        raise PlayerInputError(
            f"Custom Steam URL /id/{match.group(1)} cannot be resolved without a Steam Web API "
            "key. Use the numeric account ID shown on OpenDota, Dotabuff or STRATZ, or a "
            "steamcommunity.com/profiles/<SteamID64> link."
        )
    for pattern in _ID_URLS:
        if match := pattern.search(text):
            return _account_from_number(int(match.group(1)))
    if match := _LEGACY_ID.fullmatch(text):
        return _account_from_number(int(match.group(2)) * 2 + int(match.group(1)))
    if match := _STEAM3_ID.fullmatch(text):
        return _account_from_number(int(match.group(1)))
    if text.isdigit():
        return _account_from_number(int(text))
    raise PlayerInputError(
        f"{text!r} is not an account ID, SteamID64 or Steam/OpenDota/STRATZ/Dotabuff profile "
        "URL. Searching players by name is not supported."
    )


# Data model


@dataclass(frozen=True, slots=True)
class PlayerInfo:
    account_id: int
    persona_name: str | None
    pro_name: str | None
    rank_tier: int | None
    leaderboard_rank: int | None
    steam_id64: str | None
    profile_url: str | None
    history_incomplete: bool

    @property
    def bracket(self) -> int | None:
        """Medal 1 (Herald) to 8 (Immortal), or None when uncalibrated or hidden."""
        if self.rank_tier and 1 <= self.rank_tier // 10 <= 8:
            return self.rank_tier // 10
        return None


@dataclass(frozen=True, slots=True)
class PlayerMatch:
    match_id: int
    start_time: datetime
    duration_s: int
    hero_id: int
    win: bool
    radiant: bool
    kills: int
    deaths: int
    assists: int
    gpm: int
    xpm: int
    last_hits: int
    denies: int
    hero_damage: int | None
    tower_damage: int | None
    hero_healing: int | None
    party_size: int | None
    parsed: bool
    position: Position | None = None
    position_inferred: bool = False
    # From STRATZ, parsed matches only: index m is the value at minute m.
    networth_by_minute: tuple[int, ...] = ()
    xp_by_minute: tuple[int, ...] = ()

    @property
    def minutes(self) -> float:
        return max(self.duration_s, 60) / 60

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)


def match_from_opendota(row: dict[str, Any]) -> PlayerMatch:
    radiant = row["player_slot"] < 128
    return PlayerMatch(
        match_id=row["match_id"],
        start_time=datetime.fromtimestamp(row["start_time"], UTC),
        duration_s=row.get("duration") or 0,
        hero_id=row["hero_id"],
        win=radiant == bool(row["radiant_win"]),
        radiant=radiant,
        kills=row.get("kills") or 0,
        deaths=row.get("deaths") or 0,
        assists=row.get("assists") or 0,
        gpm=row.get("gold_per_min") or 0,
        xpm=row.get("xp_per_min") or 0,
        last_hits=row.get("last_hits") or 0,
        denies=row.get("denies") or 0,
        hero_damage=row.get("hero_damage"),
        tower_damage=row.get("tower_damage"),
        hero_healing=row.get("hero_healing"),
        party_size=row.get("party_size"),
        parsed=row.get("version") is not None,
    )


_INFERRED_POSITION = {
    ("CORE", "SAFE_LANE"): Position.CARRY,
    ("CORE", "MID_LANE"): Position.MID,
    ("CORE", "OFF_LANE"): Position.OFFLANE,
}


def _stratz_position(player: dict[str, Any]) -> tuple[Position | None, bool]:
    """STRATZ's position, or one inferred from lane and role when it is missing."""
    position = player.get("position")
    if position and position.startswith("POSITION_"):
        return Position(int(position.removeprefix("POSITION_"))), False
    role, lane = player.get("role"), player.get("lane")
    if role == "HARD_SUPPORT":
        return Position.HARD_SUPPORT, True
    if role == "LIGHT_SUPPORT":
        return Position.SOFT_SUPPORT, True
    inferred = _INFERRED_POSITION.get((role, lane))
    return inferred, inferred is not None


def _cumulative(values: Iterable[int]) -> tuple[int, ...]:
    total, out = 0, []
    for value in values:
        total += value
        out.append(total)
    return tuple(out)


def apply_stratz(match: PlayerMatch, detail: dict[str, Any] | None) -> PlayerMatch:
    if not detail or not detail.get("players"):
        return match
    player = detail["players"][0]
    position, inferred = _stratz_position(player)
    stats = player.get("stats") or {}
    networth = tuple(stats.get("networthPerMinute") or ())
    # experiencePerMinute holds XP gained in each minute; XP at minute m is the sum of the
    # first m entries, so prefix a 0 to make index m mean "at minute m".
    xp = (0, *_cumulative(stats.get("experiencePerMinute") or ())) if stats else ()
    return replace(
        match,
        position=position,
        position_inferred=inferred,
        networth_by_minute=networth,
        xp_by_minute=xp if len(xp) > 1 else (),
    )


def hero_name(hero_id: int) -> str:
    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"hero {hero_id}"


def medal_name(rank_tier: int | None) -> str | None:
    if not rank_tier:
        return None
    medal, stars = divmod(rank_tier, 10)
    if not 1 <= medal <= 8:
        return None
    name = MEDALS[medal - 1]
    return name if medal == 8 or not stars else f"{name} {stars}"


# Source calls


@cached(ttl=TTL_PLAYER)
async def fetch_player(account_id: int) -> dict[str, Any]:
    return await get_opendota().player(account_id)


@cached(ttl=TTL_PLAYER)
async def fetch_win_loss(account_id: int) -> dict[str, Any]:
    # OpenDotaClient has no method for /wl, /recentMatches or /peers yet.
    return await get_opendota()._get(f"/players/{account_id}/wl")


@cached(ttl=TTL_PLAYER)
async def fetch_recent_matches(account_id: int) -> list[dict[str, Any]]:
    return await get_opendota()._get(f"/players/{account_id}/recentMatches")


def _mode_params(game_mode: GameMode | None) -> dict[str, int | None]:
    if game_mode is None:
        return {}
    mode, lobby = GAME_MODE_PARAMS[game_mode]
    return {"game_mode": mode, "lobby_type": lobby}


@cached(ttl=TTL_PLAYER)
async def fetch_matches(
    account_id: int,
    limit: int,
    days: int | None,
    hero_id: int | None,
    game_mode: GameMode | None,
) -> list[dict[str, Any]]:
    return await get_opendota().player_matches(
        account_id,
        limit=limit,
        date=days,
        hero_id=hero_id,
        project=list(MATCH_FIELDS),
        **_mode_params(game_mode),
    )


@cached(ttl=TTL_PLAYER)
async def fetch_peers(
    account_id: int, limit: int, days: int | None, game_mode: GameMode | None
) -> list[dict[str, Any]]:
    params = {"limit": limit, "date": days, **_mode_params(game_mode)}
    return await get_opendota()._get(
        f"/players/{account_id}/peers", {k: v for k, v in params.items() if v is not None}
    )


@cached(ttl=TTL_AGGREGATE)
async def fetch_hero_stats() -> list[dict[str, Any]]:
    return await get_opendota().hero_stats()


@cached(ttl=TTL_AGGREGATE)
async def fetch_benchmarks(hero_id: int, bracket: int | None) -> dict[str, Any]:
    # OpenDotaClient.benchmarks() does not pass ``bracket`` yet, so call the endpoint directly.
    params = {"hero_id": hero_id}
    if bracket is not None:
        params["bracket"] = bracket
    return await get_opendota()._get("/benchmarks", params)


@cached(ttl=TTL_PLAYER)
async def fetch_stratz_details(
    account_id: int, match_ids: tuple[int, ...]
) -> dict[int, dict[str, Any]]:
    details: dict[int, dict[str, Any]] = {}
    for start in range(0, len(match_ids), STRATZ_PAGE):
        chunk = list(match_ids[start : start + STRATZ_PAGE])
        data = await get_stratz().query(
            STRATZ_DETAILS_QUERY, {"id": account_id, "matchIds": chunk, "take": len(chunk)}
        )
        for match in (data.get("player") or {}).get("matches") or []:
            details[match["id"]] = match
    return details


# Loading


@dataclass(frozen=True)
class MatchQuery:
    """Which of a player's matches to analyse."""

    matches: int = 100
    days: int | None = None
    hero_id: int | None = None
    position: Position | None = None
    game_mode: GameMode | None = None

    def describe(self) -> str:
        parts = [f"last {self.matches} matches"]
        if self.days:
            parts.append(f"within {self.days} days")
        if self.hero_id:
            parts.append(f"on {hero_name(self.hero_id)}")
        if self.position:
            parts.append(f"at position {int(self.position)}")
        if self.game_mode:
            parts.append(f"in {self.game_mode.value}")
        return ", ".join(parts)


@dataclass
class MatchSet:
    player: PlayerInfo
    query: MatchQuery
    matches: list[PlayerMatch]  # newest first
    stratz_used: bool = False
    caveats: list[str] = field(default_factory=list)

    @property
    def sources(self) -> list[Source]:
        return ["opendota", "stratz"] if self.stratz_used else ["opendota"]


async def load_player(account_id: int) -> PlayerInfo:
    try:
        data = await fetch_player(account_id)
    except OpenDotaNotFoundError:
        raise PlayerUnavailable(
            f"OpenDota has no player with account ID {account_id}. Check the ID; "
            "only accounts that have played public matches are known."
        ) from None
    profile = (data or {}).get("profile")
    if not profile:
        raise PlayerUnavailable(
            f"Account {account_id} is unknown to OpenDota or its profile is private."
        )
    history_incomplete = bool(profile.get("fh_unavailable"))
    if history_incomplete:
        wl = await fetch_win_loss(account_id)
        if not (wl.get("win", 0) + wl.get("lose", 0)):
            raise PlayerUnavailable(
                f"Account {account_id} keeps its match data private (the in-game "
                '"Expose Public Match Data" option is off), so no statistics are available.'
            )
    return PlayerInfo(
        account_id=account_id,
        persona_name=profile.get("personaname"),
        pro_name=profile.get("name"),
        rank_tier=data.get("rank_tier"),
        leaderboard_rank=data.get("leaderboard_rank"),
        steam_id64=profile.get("steamid"),
        profile_url=profile.get("profileurl"),
        history_incomplete=history_incomplete,
    )


async def load_matches(account_id: int, query: MatchQuery, *, details: bool = False) -> MatchSet:
    """The player's matches for ``query``, with STRATZ details when asked or needed."""
    player = await load_player(account_id)
    rows = await fetch_matches(
        account_id, query.matches, query.days, query.hero_id, query.game_mode
    )
    matches = [match_from_opendota(r) for r in rows if r.get("hero_id")]
    result = MatchSet(player, query, matches, caveats=list(COMMON_CAVEATS))
    if player.history_incomplete:
        result.caveats.append(
            "OpenDota could not fetch this account's full match history; older matches "
            "may be missing."
        )
    if (details or query.position) and matches:
        try:
            stratz = await fetch_stratz_details(account_id, tuple(m.match_id for m in matches))
        except StratzError as exc:
            if query.position:
                raise PlayerQueryError(
                    f"Filtering by position needs STRATZ, which failed: {exc}"
                ) from exc
            result.caveats.append(
                f"STRATZ was unavailable ({type(exc).__name__}), so positions and "
                "per-minute net worth/XP are left out."
            )
        else:
            result.matches = [apply_stratz(m, stratz.get(m.match_id)) for m in matches]
            result.stratz_used = True
            result.caveats.append(STRATZ_CAVEAT)
            inferred = sum(m.position_inferred for m in result.matches)
            if inferred:
                result.caveats.append(
                    f"{inferred} match positions were inferred from STRATZ lane and role "
                    "because the match was not parsed."
                )
    if query.position:
        before = len(result.matches)
        result.matches = [m for m in result.matches if m.position == query.position]
        result.caveats.append(
            f"{len(result.matches)} of the last {before} matches were at position "
            f"{int(query.position)}; matches with unknown position are excluded."
        )
    return result


# Report container and shared helpers


@dataclass
class Report:
    result: dict[str, Any]
    sample_size: int
    period: Period | None
    definitions: dict[str, str]
    confidence: dict[str, tuple[float, float]]
    caveats: list[str]
    sources: list[Source]
    data_freshness: datetime | None


def period_of(matches: Sequence[PlayerMatch]) -> Period | None:
    if not matches:
        return None
    dates = [m.start_time.date() for m in matches]
    return Period(from_date=min(dates), to_date=max(dates))


def freshness_of(matches: Sequence[PlayerMatch]) -> datetime | None:
    if not matches:
        return None
    latest = max(matches, key=lambda m: m.start_time)
    return datetime.fromtimestamp(latest.start_time.timestamp() + latest.duration_s, UTC)


def require(matches: Sequence[PlayerMatch], min_sample: int, query: MatchQuery) -> None:
    if len(matches) < min_sample:
        raise InsufficientData(
            f"Only {len(matches)} matches for {query.describe()}; at least {min_sample} needed.",
            "Ask for more matches or days, drop the hero or position filter, or lower "
            "min_sample (noisier).",
            sample_size=len(matches),
        )


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _wins(matches: Iterable[PlayerMatch]) -> int:
    return sum(m.win for m in matches)


def win_rate_block(matches: Sequence[PlayerMatch]) -> dict[str, Any]:
    n, wins = len(matches), _wins(matches)
    low, high = wilson_interval(wins, n)
    return {
        "matches": n,
        "wins": wins,
        "win_rate": _r(wins / n, 4) if n else None,
        "win_rate_ci": [_r(low, 4), _r(high, 4)],
    }


AVERAGE_METRICS: dict[str, tuple[Callable[[PlayerMatch], float | None], str]] = {
    "kills": (lambda m: m.kills, "Kills per match."),
    "deaths": (lambda m: m.deaths, "Deaths per match."),
    "assists": (lambda m: m.assists, "Assists per match."),
    "kda": (lambda m: m.kda, "(kills + assists) / max(1, deaths), averaged over matches."),
    "gpm": (lambda m: m.gpm, "Gold per minute at the end of the match."),
    "xpm": (lambda m: m.xpm, "Experience per minute at the end of the match."),
    "last_hits": (lambda m: m.last_hits, "Last hits per match."),
    "denies": (lambda m: m.denies, "Denies per match."),
    "hero_damage": (lambda m: m.hero_damage, "Damage dealt to enemy heroes per match."),
    "tower_damage": (lambda m: m.tower_damage, "Damage dealt to buildings per match."),
    "hero_healing": (lambda m: m.hero_healing, "Healing done to heroes per match."),
    "duration_min": (lambda m: m.duration_s / 60, "Match length in minutes."),
}


def _values(matches: Iterable[PlayerMatch], getter: Callable[[PlayerMatch], Any]) -> list[float]:
    return [v for v in (getter(m) for m in matches) if v is not None]


def averages(
    matches: Sequence[PlayerMatch], *, with_ci: bool = False
) -> tuple[dict[str, float | None], dict[str, tuple[float, float]]]:
    means: dict[str, float | None] = {}
    intervals: dict[str, tuple[float, float]] = {}
    for name, (getter, _) in AVERAGE_METRICS.items():
        values = _values(matches, getter)
        means[name] = _r(fmean(values)) if values else None
        if with_ci and len(values) >= 2:
            low, high = bootstrap_ci(values, stat=fmean, resamples=BOOTSTRAP_RESAMPLES, seed=0)
            intervals[name] = (round(low, 2), round(high, 2))
    return means, intervals


def average_definitions() -> dict[str, str]:
    return {f"avg_{k}": v for k, (_, v) in AVERAGE_METRICS.items()} | {
        "win_rate": "Wins / matches over the selected matches.",
        "win_rate_ci": "95% Wilson score interval for the win rate.",
    }


def player_block(player: PlayerInfo) -> dict[str, Any]:
    return {
        "account_id": player.account_id,
        "persona_name": player.persona_name,
        "pro_name": player.pro_name,
        "rank_tier": player.rank_tier,
        "medal": medal_name(player.rank_tier),
        "leaderboard_rank": player.leaderboard_rank,
    }


def _report(ms: MatchSet, result: dict[str, Any], **extra: Any) -> Report:
    return Report(
        result=result,
        sample_size=extra.pop("sample_size", len(ms.matches)),
        period=period_of(ms.matches),
        definitions=extra.pop("definitions", {}),
        confidence=extra.pop("confidence", {}),
        caveats=ms.caveats + extra.pop("caveats", []),
        sources=ms.sources,
        data_freshness=freshness_of(ms.matches),
    )


# Profile


async def profile_report(account_id: int) -> Report:
    player = await load_player(account_id)
    wl, recent_rows = await asyncio.gather(
        fetch_win_loss(account_id), fetch_recent_matches(account_id)
    )
    wins, losses = wl.get("win", 0), wl.get("lose", 0)
    total = wins + losses
    if not total:
        raise PlayerUnavailable(f"Account {account_id} has no public matches on OpenDota.")
    recent = [match_from_opendota(r) for r in recent_rows if r.get("hero_id")]
    low, high = wilson_interval(wins, total)
    recent_means, _ = averages(recent)
    heroes = Counter(m.hero_id for m in recent)
    result = {
        "player": player_block(player)
        | {"steam_id64": player.steam_id64, "profile_url": player.profile_url},
        "overall": {
            "matches": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 4),
            "win_rate_ci": [round(low, 4), round(high, 4)],
        },
        "last_20": {
            **win_rate_block(recent),
            "averages": recent_means,
            "most_played_heroes": [
                {"hero": hero_name(h), "matches": n} for h, n in heroes.most_common(3)
            ],
            "games": [
                {
                    "match_id": m.match_id,
                    "start_time": m.start_time.isoformat(),
                    "hero": hero_name(m.hero_id),
                    "result": "win" if m.win else "loss",
                    "kills": m.kills,
                    "deaths": m.deaths,
                    "assists": m.assists,
                    "gpm": m.gpm,
                    "xpm": m.xpm,
                    "duration_min": round(m.duration_s / 60, 1),
                }
                for m in recent
            ],
        },
    }
    caveats = list(COMMON_CAVEATS)
    if player.rank_tier is None:
        caveats.append("No medal is shown: the player is uncalibrated or hides it.")
    if player.history_incomplete:
        caveats.append("OpenDota could not fetch the full match history; totals may be low.")
    return Report(
        result=result,
        sample_size=total,
        period=period_of(recent),
        definitions={
            "rank_tier": "OpenDota medal code: tens digit = medal (1 Herald … 8 Immortal), "
            "units digit = stars.",
            "leaderboard_rank": "Position on Valve's regional Immortal leaderboard.",
            "overall.win_rate": "All-time wins / matches known to OpenDota, every game mode.",
            "overall.win_rate_ci": "95% Wilson score interval.",
            "last_20": "The 20 most recent matches, every game mode.",
        },
        confidence={"overall.win_rate": (round(low, 4), round(high, 4))},
        caveats=caveats,
        sources=["opendota"],
        data_freshness=freshness_of(recent),
    )


# Averages


async def stats_report(account_id: int, query: MatchQuery, min_sample: int) -> Report:
    ms = await load_matches(account_id, query)
    require(ms.matches, min_sample, query)
    means, intervals = averages(ms.matches, with_ci=True)
    block = win_rate_block(ms.matches)
    heroes = Counter(m.hero_id for m in ms.matches)
    confidence = {"win_rate": tuple(block["win_rate_ci"])} | {
        f"avg_{k}": v for k, v in intervals.items()
    }
    caveats = ["Averages mix every hero and role in the selection; compare like with like."]
    return _report(
        ms,
        {
            "player": player_block(ms.player),
            "selection": query.describe(),
            **block,
            "averages": means,
            "heroes_played": len(heroes),
            "most_played_heroes": [
                {"hero": hero_name(h), "matches": n} for h, n in heroes.most_common(5)
            ],
        },
        definitions=average_definitions()
        | {"avg_* confidence": "95% percentile-bootstrap interval of the mean."},
        confidence=confidence,
        caveats=caveats,
    )


# Benchmarks


BENCHMARK_METRICS: dict[str, tuple[Callable[[PlayerMatch], float | None], bool]] = {
    # benchmark key: (per-match value, higher is better)
    "gold_per_min": (lambda m: m.gpm, True),
    "xp_per_min": (lambda m: m.xpm, True),
    "last_hits_per_min": (lambda m: m.last_hits / m.minutes, True),
    "hero_damage_per_min": (
        lambda m: None if m.hero_damage is None else m.hero_damage / m.minutes,
        True,
    ),
    "kills_per_min": (lambda m: m.kills / m.minutes, True),
    "deaths_per_min": (lambda m: m.deaths / m.minutes, False),
    "assists_per_min": (lambda m: m.assists / m.minutes, True),
}
FARM_METRICS = ("gold_per_min", "xp_per_min", "last_hits_per_min", "hero_damage_per_min")


def percentile_of(table: Sequence[dict[str, Any]], value: float) -> float | None:
    """Percentile of ``value`` in a benchmark table, linearly interpolated.

    Values below the lowest published percentile (p10) or above the highest (p99) are
    clamped to it.
    """
    points = sorted(
        (row["percentile"], row["value"]) for row in table if row.get("value") is not None
    )
    if not points:
        return None
    if value <= points[0][1]:
        return points[0][0]
    for (p0, v0), (p1, v1) in zip(points, points[1:], strict=False):
        if value <= v1:
            return p1 if v1 == v0 else p0 + (p1 - p0) * (value - v0) / (v1 - v0)
    return points[-1][0]


def benchmark_bracket(player: PlayerInfo) -> int | None:
    """OpenDota benchmark bracket for the player: their medal, Immortal mapped to Divine."""
    bracket = player.bracket
    return None if bracket is None else min(bracket, 7)


def bracket_caveats(player: PlayerInfo) -> list[str]:
    if player.bracket is None:
        return ["The player has no visible medal, so benchmarks are for all ranks."]
    if player.bracket == 8:
        return [
            "OpenDota has no Immortal benchmarks or hero win rates, so Divine is used for "
            "this Immortal player; Immortal players will usually sit above Divine medians."
        ]
    return []


async def load_benchmarks(
    hero_ids: Iterable[int], bracket: int | None
) -> dict[int, dict[str, list[dict[str, Any]]]]:
    async def one(hero_id: int) -> tuple[int, dict | None]:
        try:
            data = await fetch_benchmarks(hero_id, bracket)
        except OpenDotaError:
            return hero_id, None
        return hero_id, (data or {}).get("result")

    pairs = await asyncio.gather(*(one(h) for h in hero_ids))
    return {h: r for h, r in pairs if r}


def performance_percentiles(
    match: PlayerMatch, bench: dict[str, list[dict[str, Any]]]
) -> dict[str, float]:
    """Per-metric percentile where higher is better (deaths are inverted)."""
    out = {}
    for key, (getter, higher_better) in BENCHMARK_METRICS.items():
        value = getter(match)
        table = bench.get(key)
        if value is None or not table:
            continue
        pct = percentile_of(table, value)
        if pct is not None:
            out[key] = pct if higher_better else 1 - pct
    return out


def hero_rank_win_rate(row: dict[str, Any] | None, bracket: int | None) -> tuple[int, int] | None:
    """(wins, picks) for a hero at the bracket from /heroStats; Immortal falls back to Divine."""
    if not row:
        return None
    if bracket is None:
        wins, picks = row.get("pub_win"), row.get("pub_pick")
    else:
        b = bracket if row.get(f"{bracket}_pick") else min(bracket, 7)
        wins, picks = row.get(f"{b}_win"), row.get(f"{b}_pick")
    return (wins, picks) if picks else None


BENCHMARK_DEFINITIONS = {
    "benchmark_percentile": "Where the player's per-match value falls among OpenDota "
    "/benchmarks for the same hero at the player's medal (0.5 = that rank's median). "
    "Interpolated between published percentiles and clamped to p10-p99. For deaths the "
    "percentile is inverted so higher is always better.",
}


# Hero pool


async def hero_pool_report(
    account_id: int, query: MatchQuery, min_sample: int, min_hero_matches: int
) -> Report:
    ms = await load_matches(account_id, query)
    require(ms.matches, min_sample, query)
    by_hero: dict[int, list[PlayerMatch]] = defaultdict(list)
    for m in ms.matches:
        by_hero[m.hero_id].append(m)
    ranked = sorted(by_hero.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    eligible = [h for h, games in ranked if len(games) >= min_hero_matches]
    bracket = benchmark_bracket(ms.player)
    hero_stats, benches = await asyncio.gather(
        fetch_hero_stats(), load_benchmarks(eligible[:MAX_BENCHMARK_HEROES], bracket)
    )
    stats_by_id = {row["id"]: row for row in hero_stats}

    rows, tests = [], []
    for hero_id, games in ranked:
        row: dict[str, Any] = {"hero": hero_name(hero_id), **win_rate_block(games)}
        means, _ = averages(games)
        row["avg_kda"], row["avg_gpm"], row["avg_xpm"] = means["kda"], means["gpm"], means["xpm"]
        rank_rate = hero_rank_win_rate(stats_by_id.get(hero_id), ms.player.bracket)
        row["rank_win_rate"] = _r(rank_rate[0] / rank_rate[1], 4) if rank_rate else None
        if hero_id in benches:
            per_match = [performance_percentiles(m, benches[hero_id]) for m in games]
            medians = {
                key: round(median(vals), 3)
                for key in BENCHMARK_METRICS
                if (vals := [p[key] for p in per_match if key in p])
            }
            row["benchmark_percentile"] = medians
            farm = [medians[k] for k in FARM_METRICS if k in medians]
            row["performance_index"] = round(fmean(farm), 3) if farm else None
        if len(games) >= min_hero_matches and rank_rate:
            p = two_proportion_ztest(row["wins"], len(games), *rank_rate).p_value
            tests.append((len(rows), p))
        rows.append(row)

    if tests:
        bh = benjamini_hochberg([p for _, p in tests])
        for (i, _), q, rejected in zip(tests, bh.adjusted, bh.rejected, strict=True):
            rows[i]["win_rate_q_value"] = round(q, 4)
            rows[i]["win_rate_differs_from_rank"] = rejected

    over, under = [], []
    for row in rows:
        if row["matches"] < min_hero_matches:
            continue
        reasons_up, reasons_down = [], []
        if row.get("win_rate_differs_from_rank"):
            target = reasons_up if row["win_rate"] > row["rank_win_rate"] else reasons_down
            target.append(
                f"win rate {row['win_rate']:.1%} vs {row['rank_win_rate']:.1%} at rank "
                f"(q={row['win_rate_q_value']})"
            )
        index = row.get("performance_index")
        if index is not None and index >= 0.6:
            reasons_up.append(f"farm/damage benchmarks at p{round(index * 100)} for the rank")
        if index is not None and index <= 0.4:
            reasons_down.append(f"farm/damage benchmarks at p{round(index * 100)} for the rank")
        if reasons_up:
            over.append({"hero": row["hero"], "matches": row["matches"], "reasons": reasons_up})
        if reasons_down:
            under.append({"hero": row["hero"], "matches": row["matches"], "reasons": reasons_down})

    caveats = bracket_caveats(ms.player) + [
        f"Benchmarks are fetched for at most {MAX_BENCHMARK_HEROES} most played heroes with "
        f"at least {min_hero_matches} matches; others have none.",
        "Benchmarks cover every role a hero is played in.",
        "Rank win rates are OpenDota /heroStats for the player's medal over recent days, "
        "not the same period as the player's matches.",
    ]
    confidence = {
        f"win_rate[{r['hero']}]": tuple(r["win_rate_ci"])
        for r in rows
        if r["matches"] >= min_hero_matches
    }
    return _report(
        ms,
        {
            "player": player_block(ms.player),
            "selection": query.describe(),
            "benchmark_bracket": medal_name(bracket * 10) if bracket else "all ranks",
            "heroes": rows,
            "overperforms": over,
            "underperforms": under,
        },
        definitions=average_definitions()
        | BENCHMARK_DEFINITIONS
        | {
            "rank_win_rate": "The hero's win rate at the player's medal (OpenDota /heroStats).",
            "performance_index": "Mean benchmark percentile of GPM, XPM, last hits/min and "
            "hero damage/min.",
            "win_rate_q_value": "Benjamini-Hochberg adjusted p-value of a two-proportion "
            "z-test against rank_win_rate, across this player's heroes.",
            "overperforms": f"Heroes with >= {min_hero_matches} matches whose win rate is "
            "significantly above rank (q <= 0.05) or performance_index >= 0.6.",
            "underperforms": "Same rules, below rank or performance_index <= 0.4.",
        },
        confidence=confidence,
        caveats=caveats,
    )


# Weaknesses


def _mean_diff_ci(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    """mean(a) - mean(b) and its 95% Welch normal-approximation interval."""
    diff = fmean(a) - fmean(b)
    se = math.sqrt(stdev(a) ** 2 / len(a) + stdev(b) ** 2 / len(b))
    return diff, diff - 1.96 * se, diff + 1.96 * se


PHASES = {10: "laning (0-10 min)", 20: "mid game (10-20 min)", 30: "late game (20-30 min)"}


def phase_divergence(matches: Sequence[PlayerMatch]) -> dict[str, Any]:
    """Net worth and XP at 10/20/30 minutes in wins vs losses, from STRATZ parsed matches."""
    parsed = [m for m in matches if m.networth_by_minute]
    out: dict[str, Any] = {"parsed_matches": len(parsed), "checkpoints": []}
    rel_prev = {"networth": 0.0, "xp": 0.0}
    growth: dict[str, list[tuple[float, str]]] = {"networth": [], "xp": []}
    for minute in PHASE_MINUTES:
        point: dict[str, Any] = {"minute": minute}
        for metric, attr in (("networth", "networth_by_minute"), ("xp", "xp_by_minute")):
            wins = [
                getattr(m, attr)[minute] for m in parsed if m.win and len(getattr(m, attr)) > minute
            ]
            losses = [
                getattr(m, attr)[minute]
                for m in parsed
                if not m.win and len(getattr(m, attr)) > minute
            ]
            entry: dict[str, Any] = {"wins_n": len(wins), "losses_n": len(losses)}
            if len(wins) >= MIN_PHASE_GROUP and len(losses) >= MIN_PHASE_GROUP:
                diff, low, high = _mean_diff_ci(wins, losses)
                rel = diff / fmean(wins) if fmean(wins) else 0.0
                entry |= {
                    "mean_in_wins": round(fmean(wins)),
                    "mean_in_losses": round(fmean(losses)),
                    "gap": round(diff),
                    "gap_ci": [round(low), round(high)],
                    "relative_gap": round(rel, 3),
                    "significant": low > 0 or high < 0,
                }
                growth[metric].append((rel - rel_prev[metric], PHASES[minute]))
                rel_prev[metric] = rel
            point[metric] = entry
        out["checkpoints"].append(point)
    for metric, steps in growth.items():
        out[f"{metric}_diverges_most_in"] = max(steps)[1] if steps else None
    return out


def _trait_rows(
    matches: Sequence[PlayerMatch], traits: dict[str, Callable[[PlayerMatch], bool | None]]
) -> list[dict[str, Any]]:
    rows, tests = [], []
    wins_total = [m for m in matches if m.win]
    losses_total = [m for m in matches if not m.win]
    for name, test in traits.items():
        known = [(m, t) for m in matches if (t := test(m)) is not None]
        inside = [m for m, t in known if t]
        outside = [m for m, t in known if not t]
        if not inside or not outside:
            continue
        row = {
            "trait": name,
            "share_of_losses": _r(sum(not m.win for m in inside) / len(losses_total), 3)
            if losses_total
            else None,
            "share_of_wins": _r(sum(m.win for m in inside) / len(wins_total), 3)
            if wins_total
            else None,
            "with_trait": win_rate_block(inside),
            "without_trait": win_rate_block(outside),
        }
        tests.append(
            two_proportion_ztest(_wins(inside), len(inside), _wins(outside), len(outside)).p_value
        )
        rows.append(row)
    if rows:
        bh = benjamini_hochberg(tests)
        for row, q, rejected in zip(rows, bh.adjusted, bh.rejected, strict=True):
            row["q_value"] = round(q, 4)
            row["significant"] = rejected
    return sorted(rows, key=lambda r: r["q_value"])


async def weaknesses_report(account_id: int, query: MatchQuery, min_sample: int) -> Report:
    ms = await load_matches(account_id, query, details=True)
    require(ms.matches, min_sample, query)
    matches = ms.matches
    heroes = Counter(m.hero_id for m in matches)
    top = [h for h, _ in heroes.most_common(MAX_BENCHMARK_HEROES)]
    benches = await load_benchmarks(top, benchmark_bracket(ms.player))

    per_metric: dict[str, list[float]] = defaultdict(list)
    covered = 0
    for m in matches:
        if m.hero_id in benches:
            covered += 1
            for key, pct in performance_percentiles(m, benches[m.hero_id]).items():
                per_metric[key].append(pct)
    metrics, confidence = [], {}
    for key, values in per_metric.items():
        med = median(values)
        low, high = bootstrap_ci(values, resamples=BOOTSTRAP_RESAMPLES, seed=0)
        confidence[f"benchmark_percentile[{key}]"] = (round(low, 3), round(high, 3))
        metrics.append(
            {
                "metric": key,
                "median_percentile": round(med, 3),
                "ci": [round(low, 3), round(high, 3)],
                "matches": len(values),
                "below_rank_median": high < 0.5,
            }
        )
    metrics.sort(key=lambda r: r["median_percentile"])

    comfort = set(top)
    positions_known = any(m.position for m in matches)
    traits: dict[str, Callable[[PlayerMatch], bool | None]] = {
        "playing in a party": lambda m: None if m.party_size is None else m.party_size > 1,
        "dire side": lambda m: not m.radiant,
        "game shorter than 25 min": lambda m: m.duration_s < 25 * 60,
        "game longer than 45 min": lambda m: m.duration_s > 45 * 60,
        f"hero outside the {len(comfort)} most played": lambda m: m.hero_id not in comfort,
    }
    if positions_known:
        for pos in Position:
            traits[f"position {int(pos)}"] = lambda m, pos=pos: (
                None if m.position is None else m.position == pos
            )
    wins = [m for m in matches if m.win]
    losses = [m for m in matches if not m.win]
    loss_profile = {
        "wins": averages(wins)[0] if wins else None,
        "losses": averages(losses)[0] if losses else None,
    }

    caveats = bracket_caveats(ms.player) + [
        f"Benchmark percentiles cover {covered} of {len(matches)} matches (the "
        f"{len(benches)} most played heroes with OpenDota benchmarks).",
        "Win-vs-loss comparisons are correlational: the winning side naturally has more "
        "gold and XP, and heroes and roles are mixed.",
    ]
    phases = phase_divergence(matches) if ms.stratz_used else None
    if phases is not None and phases["parsed_matches"] < 2 * MIN_PHASE_GROUP:
        caveats.append(
            f"Only {phases['parsed_matches']} matches have per-minute data, too few for "
            "a phase comparison."
        )
    return _report(
        ms,
        {
            "player": player_block(ms.player),
            "selection": query.describe(),
            **win_rate_block(matches),
            "metrics_vs_rank": metrics,
            "below_rank_median": [r["metric"] for r in metrics if r["below_rank_median"]],
            "phases": phases,
            "loss_traits": _trait_rows(matches, traits),
            "loss_profile": loss_profile,
        },
        definitions=BENCHMARK_DEFINITIONS
        | {
            "median_percentile": "Median benchmark percentile over the covered matches; its "
            "ci is a 95% percentile-bootstrap interval.",
            "below_rank_median": "Metrics whose interval lies entirely below 0.5.",
            "phases": "STRATZ net worth and cumulative XP at 10, 20 and 30 minutes in wins vs "
            "losses (parsed matches lasting that long). gap_ci is a 95% Welch interval; "
            "relative_gap = gap / mean in wins. *_diverges_most_in names the phase where the "
            "relative gap grows most.",
            "loss_traits": "Win rate with and without a trait, two-proportion z-test, "
            "Benjamini-Hochberg adjusted (q_value); share_of_losses = losses with the trait "
            "/ all losses.",
            "loss_profile": "Average stats in won and lost matches.",
        },
        confidence=confidence,
        caveats=caveats,
    )


# Trends


def streaks(matches: Sequence[PlayerMatch]) -> dict[str, Any]:
    """Longest and current win/loss streaks; ``matches`` must be oldest first."""
    longest = {True: 0, False: 0}
    run_kind, run = None, 0
    for m in matches:
        run = run + 1 if m.win == run_kind else 1
        run_kind = m.win
        longest[m.win] = max(longest[m.win], run)
    return {
        "longest_win_streak": longest[True],
        "longest_loss_streak": longest[False],
        "current_streak": {
            "kind": None if run_kind is None else ("win" if run_kind else "loss"),
            "length": run,
        },
    }


def _slope(values: Sequence[float]) -> float | None:
    """Least-squares slope of values against their index."""
    n = len(values)
    if n < 2:
        return None
    mean_x, mean_y = (n - 1) / 2, fmean(values)
    sxx = sum((i - mean_x) ** 2 for i in range(n))
    return sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values)) / sxx


TREND_METRICS = ("kda", "gpm", "xpm", "last_hits", "hero_damage", "deaths")


async def trends_report(
    account_id: int, query: MatchQuery, min_sample: int, bucket_size: int
) -> Report:
    ms = await load_matches(account_id, query)
    require(ms.matches, min_sample, query)
    ordered = sorted(ms.matches, key=lambda m: m.start_time)
    buckets = []
    for start in range(0, len(ordered), bucket_size):
        chunk = ordered[start : start + bucket_size]
        means, _ = averages(chunk)
        buckets.append(
            {
                "from": chunk[0].start_time.date().isoformat(),
                "to": chunk[-1].start_time.date().isoformat(),
                **win_rate_block(chunk),
                **{f"avg_{k}": means[k] for k in TREND_METRICS},
            }
        )
    slopes = {}
    for key in TREND_METRICS:
        values = _values(ordered, AVERAGE_METRICS[key][0])
        slope = _slope(values)
        slopes[key] = None if slope is None else round(slope * 10, 3)
    half = len(ordered) // 2
    first, second = ordered[:half], ordered[half:]
    halves = {"first_half": win_rate_block(first), "second_half": win_rate_block(second)}
    if first and second:
        test = two_proportion_ztest(_wins(second), len(second), _wins(first), len(first))
        halves["p_value"] = round(test.p_value, 4)
    return _report(
        ms,
        {
            "player": player_block(ms.player),
            "selection": query.describe(),
            **win_rate_block(ordered),
            "buckets": buckets,
            "change_per_10_matches": slopes,
            "win_rate_halves": halves,
            "streaks": streaks(ordered),
        },
        definitions=average_definitions()
        | {
            "buckets": f"Consecutive groups of {bucket_size} matches, oldest first.",
            "change_per_10_matches": "Least-squares slope of each per-match stat against "
            "match order, times 10; positive means rising.",
            "win_rate_halves": "Older vs newer half of the matches; p_value from a "
            "two-proportion z-test.",
            "streaks": "Consecutive wins or losses in match order; current_streak ends with "
            "the latest match.",
        },
        caveats=[
            "Trends mix heroes and roles; a shift can reflect a changed hero pool.",
            "Small buckets have wide win-rate intervals (see each bucket's win_rate_ci).",
        ],
    )


# Peers


def _peer_row(peer: dict[str, Any], games: int, wins: int, base: float | None) -> dict[str, Any]:
    low, high = wilson_interval(wins, games)
    rate = wins / games
    return {
        "account_id": peer.get("account_id"),
        "persona_name": peer.get("personaname"),
        "pro_name": peer.get("name"),
        "matches": games,
        "wins": wins,
        "win_rate": round(rate, 4),
        "win_rate_ci": [round(low, 4), round(high, 4)],
        "vs_player_average": None if base is None else round(rate - base, 4),
    }


async def peers_report(account_id: int, query: MatchQuery, min_games: int, top: int) -> Report:
    ms = await load_matches(account_id, query)
    require(ms.matches, 1, query)
    base = _wins(ms.matches) / len(ms.matches)
    rows = await fetch_peers(account_id, query.matches, query.days, query.game_mode)
    teammates = sorted(
        (p for p in rows if p.get("with_games", 0) >= min_games),
        key=lambda p: (-p["with_games"], p.get("account_id", 0)),
    )[:top]
    opponents = sorted(
        (p for p in rows if p.get("against_games", 0) >= min_games),
        key=lambda p: (-p["against_games"], p.get("account_id", 0)),
    )[:top]
    result = {
        "player": player_block(ms.player),
        "selection": query.describe(),
        **win_rate_block(ms.matches),
        "teammates": [_peer_row(p, p["with_games"], p["with_win"], base) for p in teammates],
        "opponents": [_peer_row(p, p["against_games"], p["against_win"], base) for p in opponents],
    }
    return _report(
        ms,
        result,
        definitions={
            "teammates": f"Players on the same team in >= {min_games} of the selected matches; "
            "win_rate is this player's win rate with them.",
            "opponents": f"Players on the enemy team in >= {min_games} matches; win_rate is "
            "this player's win rate against them.",
            "vs_player_average": "Peer win rate minus the player's win rate over the same matches.",
            "win_rate_ci": "95% Wilson score interval.",
        },
        caveats=[
            "Only peers who expose their match data are listed; anonymous players are not.",
            "Party games with friends and solo games against strangers are mixed.",
        ],
    )


# Comparison


async def compare_report(
    account_a: int, account_b: int, query: MatchQuery, min_sample: int, min_hero_matches: int
) -> Report:
    if account_a == account_b:
        raise PlayerQueryError("Both arguments are the same account.")
    a, b = await asyncio.gather(
        load_matches(account_a, query, details=True), load_matches(account_b, query, details=True)
    )
    for ms in (a, b):
        require(ms.matches, min_sample, query)

    def overview(ms: MatchSet) -> dict[str, Any]:
        means, _ = averages(ms.matches)
        return {
            "player": player_block(ms.player),
            **win_rate_block(ms.matches),
            "averages": {k: means[k] for k in ("kda", "gpm", "xpm", "last_hits", "hero_damage")},
        }

    def by_key(ms: MatchSet, key: Callable[[PlayerMatch], Any]) -> dict[Any, list[PlayerMatch]]:
        groups: dict[Any, list[PlayerMatch]] = defaultdict(list)
        for m in ms.matches:
            if (k := key(m)) is not None:
                groups[k].append(m)
        return groups

    def side(games: list[PlayerMatch]) -> dict[str, Any]:
        means, _ = averages(games)
        return {
            **win_rate_block(games),
            **{f"avg_{k}": means[k] for k in ("kda", "gpm", "xpm", "last_hits")},
        }

    heroes_a, heroes_b = by_key(a, lambda m: m.hero_id), by_key(b, lambda m: m.hero_id)
    shared = sorted(
        (
            h
            for h in heroes_a.keys() & heroes_b.keys()
            if len(heroes_a[h]) >= min_hero_matches and len(heroes_b[h]) >= min_hero_matches
        ),
        key=lambda h: (-(len(heroes_a[h]) + len(heroes_b[h])), h),
    )
    pos_a, pos_b = by_key(a, lambda m: m.position), by_key(b, lambda m: m.position)
    positions = []
    for pos in Position:
        if pos in pos_a or pos in pos_b:
            positions.append(
                {
                    "position": int(pos),
                    "player_a": side(pos_a[pos])
                    | {"share": round(len(pos_a[pos]) / len(a.matches), 3)},
                    "player_b": side(pos_b[pos])
                    | {"share": round(len(pos_b[pos]) / len(b.matches), 3)},
                }
            )
    caveats = []
    for label, ms in (("player_a", a), ("player_b", b)):
        caveats += [f"{label}: {c}" for c in ms.caveats if c not in COMMON_CAVEATS]
    caveats = (
        COMMON_CAVEATS
        + caveats
        + [
            "The two players' matches come from different games, opponents and ranks; "
            "differences are descriptive, not a skill test.",
        ]
    )
    stratz_used = a.stratz_used or b.stratz_used
    all_matches = a.matches + b.matches
    return Report(
        result={
            "selection": query.describe(),
            "player_a": overview(a),
            "player_b": overview(b),
            "shared_heroes": [
                {
                    "hero": hero_name(h),
                    "player_a": side(heroes_a[h]),
                    "player_b": side(heroes_b[h]),
                }
                for h in shared
            ],
            "positions": positions,
        },
        sample_size=len(all_matches),
        period=period_of(all_matches),
        definitions=average_definitions()
        | {
            "shared_heroes": f"Heroes each player played at least {min_hero_matches} times "
            "in the selection.",
            "positions": "Matches per position (STRATZ; inferred from lane and role when "
            "unparsed); share = fraction of the player's selected matches.",
        },
        confidence={
            "player_a.win_rate": tuple(win_rate_block(a.matches)["win_rate_ci"]),
            "player_b.win_rate": tuple(win_rate_block(b.matches)["win_rate_ci"]),
        },
        caveats=caveats,
        sources=["opendota", "stratz"] if stratz_used else ["opendota"],
        data_freshness=freshness_of(all_matches),
    )
