"""Pro scene data: pro matches, teams, head-to-head, tournaments, pro players, pro builds, live.

Sources, chosen to keep OpenDota calls few (the free tier is 3,000 a day):

- OpenDota ``/proMatches``, ``/teams``, ``/leagues``, ``/proPlayers``, ``/live``,
  ``/heroes/{id}/matches`` and ``/heroStats`` for lists, names and public win rates.
- OpenDota ``/explorer`` for whole-tournament and head-to-head drafts (one SQL call each,
  where the REST API would need one call per match).
- STRATZ ``team.matches``, ``player.matches`` and per-match details (drafts, item purchases,
  skill order), batched as aliased ``match(id)`` fields because ``matches(ids)`` needs an admin
  token. STRATZ has no metadata for recent leagues (``league(id)`` returns null), so league
  names always come from OpenDota.
"""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from statistics import mean, median
from typing import Any

from dota_insights.analytics.stats import bootstrap_ci, wilson_interval
from dota_insights.cache import TTL_AGGREGATE, TTL_LIVE, TTL_MATCH, TTL_REFERENCE, cached
from dota_insights.reference import hero_by_id, item_by_id
from dota_insights.reference.aliases import normalize
from dota_insights.sources import get_opendota, get_stratz
from dota_insights.sources.stratz import StratzQueryError

# /proMatches gains a row every few minutes during events.
TTL_PRO_LIST = 600
# STRATZ caps a page of team or player matches at 100.
STRATZ_PAGE = 100
MAX_TEAM_MATCHES = 300
# Aliased match(id) fields per STRATZ request.
STRATZ_BATCH = 10
EXPLORER_LIMIT = 1000
# A match on a team tag scores slightly below the same match on the team name.
TAG_FACTOR = 0.98
# Scores within this distance of the best make a name lookup ambiguous.
AMBIGUITY_MARGIN = 0.05
MIN_NAME_SCORE = 0.6
MAX_CANDIDATES = 5
# OpenDota league tiers that hold real competitions ("excluded" and "amateur" are noise).
LEAGUE_TIERS = ("premium", "professional")
# Items cheaper than this (consumables, components) are left out of build comparisons.
MIN_BUILD_ITEM_COST = 1000


class ExplorerError(Exception):
    """OpenDota's SQL explorer answered with an error (often a query timeout)."""


# Name lookup


@dataclass(frozen=True, slots=True)
class Lookup[T]:
    """Result of a fuzzy name lookup against a source list."""

    query: str
    best: T | None  # the confident match, or None when nothing or several things match
    candidates: tuple[T, ...]  # best-scoring records, best first
    namesakes: tuple[T, ...] = ()  # other records with exactly the same name as ``best``


def _name_score(query: str, key: str) -> float:
    if not query or not key:
        return 0.0
    if query == key:
        return 1.0
    if len(query) >= 2 and key.startswith(query):
        return 0.8 + 0.15 * len(query) / len(key)
    if len(query) >= 3 and query in key:
        return 0.7 + 0.15 * len(query) / len(key)
    ratio = difflib.SequenceMatcher(None, query, key).ratio()
    return ratio * 0.9 if ratio >= 0.75 else 0.0


def fuzzy_lookup[T](
    query: str,
    records: Iterable[T],
    keys: Callable[[T], Iterable[tuple[str | None, float]]],
    rank: Callable[[T], Any],
) -> Lookup[T]:
    """Score ``records`` by how well any of ``keys`` (text, weight) matches ``query``.

    An exact (normalized) name wins; several exact matches are settled by ``rank`` (higher
    is preferred) and the others are reported as namesakes. Otherwise the top match is
    accepted only if no other record scores within ``AMBIGUITY_MARGIN`` of it.
    """
    q = normalize(query)
    scored = []
    for record in records:
        score = max((_name_score(q, normalize(k)) * w for k, w in keys(record) if k), default=0.0)
        if score >= MIN_NAME_SCORE:
            scored.append((score, record))
    scored.sort(key=lambda sr: (sr[0], rank(sr[1])), reverse=True)
    candidates = tuple(r for _, r in scored[:MAX_CANDIDATES])
    if not scored:
        return Lookup(query, None, ())
    top = scored[0][0]
    if top == 1.0:
        exact = [r for s, r in scored if s == 1.0]
        return Lookup(query, exact[0], candidates, tuple(exact[1:MAX_CANDIDATES]))
    if len(scored) > 1 and scored[1][0] >= top - AMBIGUITY_MARGIN:
        return Lookup(query, None, candidates)
    return Lookup(query, scored[0][1], candidates)


async def resolve_team(query: str) -> Lookup[dict]:
    """Find a team in OpenDota's ``/teams`` list by name, tag or numeric id."""
    teams = await fetch_teams()
    if query.strip().isdigit():
        team_id = int(query)
        found = next((t for t in teams if t["team_id"] == team_id), None)
        record = found or {"team_id": team_id, "name": None, "tag": None}
        return Lookup(query, record, (record,))
    return fuzzy_lookup(
        query,
        teams,
        keys=lambda t: ((t.get("name"), 1.0), (t.get("tag"), TAG_FACTOR)),
        rank=lambda t: (t.get("last_match_time") or 0, t.get("rating") or 0),
    )


async def resolve_league(query: str) -> Lookup[dict]:
    """Find a league in OpenDota's ``/leagues`` list by name or numeric id."""
    leagues = await fetch_leagues()
    if query.strip().isdigit():
        league_id = int(query)
        found = next((lg for lg in leagues if lg["leagueid"] == league_id), None)
        record = found or {"leagueid": league_id, "name": None, "tier": None}
        return Lookup(query, record, (record,))
    return fuzzy_lookup(
        query,
        (lg for lg in leagues if lg.get("tier") in LEAGUE_TIERS),
        keys=lambda lg: ((lg.get("name"), 1.0),),
        rank=lambda lg: (lg.get("tier") == "premium", lg["leagueid"]),
    )


async def resolve_pro_player(query: str) -> Lookup[dict]:
    """Find a pro player in OpenDota's ``/proPlayers`` list by pro name or account id."""
    players = await fetch_pro_players()
    if query.strip().isdigit():
        account_id = int(query)
        found = next((p for p in players if p["account_id"] == account_id), None)
        record = found or {"account_id": account_id, "name": None}
        return Lookup(query, record, (record,))
    return fuzzy_lookup(
        query,
        players,
        keys=lambda p: ((p.get("name"), 1.0),),
        rank=lambda p: (
            bool(p.get("is_pro") or p.get("is_locked")),
            p.get("last_match_time") or "",
        ),
    )


def team_label(record: dict) -> str:
    name = record.get("name") or f"team {record.get('team_id')}"
    return f"{name} (id {record.get('team_id')})"


def league_label(record: dict) -> str:
    name = (record.get("name") or "").strip() or f"league {record.get('leagueid')}"
    return f"{name} (id {record.get('leagueid')})"


def player_label(record: dict) -> str:
    team = f", {record['team_name']}" if record.get("team_name") else ""
    return f"{record.get('name')} (account {record.get('account_id')}{team})"


# Normalized match


@dataclass(frozen=True, slots=True)
class PickBan:
    hero_id: int
    is_pick: bool
    radiant: bool
    order: int


@dataclass(slots=True)
class ProMatch:
    match_id: int
    start_time: int
    duration: int | None
    radiant_win: bool | None
    league_id: int | None = None
    league_name: str | None = None
    radiant_team_id: int | None = None
    radiant_name: str | None = None
    dire_team_id: int | None = None
    dire_name: str | None = None
    radiant_score: int | None = None
    dire_score: int | None = None
    picks_bans: list[PickBan] | None = None
    radiant_heroes: list[int] = field(default_factory=list)
    dire_heroes: list[int] = field(default_factory=list)

    def side_of(self, team_id: int) -> bool | None:
        """True if ``team_id`` played Radiant, False for Dire, None if it did not play."""
        if self.radiant_team_id == team_id:
            return True
        if self.dire_team_id == team_id:
            return False
        return None

    def heroes(self, radiant: bool) -> list[int]:
        if self.picks_bans:
            return [p.hero_id for p in self.picks_bans if p.is_pick and p.radiant == radiant]
        return self.radiant_heroes if radiant else self.dire_heroes

    def bans(self, radiant: bool) -> list[int]:
        return [p.hero_id for p in self.picks_bans or () if not p.is_pick and p.radiant == radiant]

    def first_pick_radiant(self) -> bool | None:
        picks = [p for p in self.picks_bans or () if p.is_pick]
        return min(picks, key=lambda p: p.order).radiant if picks else None


def _opendota_picks_bans(raw: list[dict] | None) -> list[PickBan] | None:
    if not raw:
        return None
    return [
        PickBan(int(p["hero_id"]), bool(p["is_pick"]), p["team"] == 0, int(p["order"])) for p in raw
    ]


def _stratz_picks_bans(raw: list[dict] | None) -> list[PickBan] | None:
    if not raw:
        return None
    result = []
    for p in raw:
        hero = p.get("heroId") if p["isPick"] else p.get("bannedHeroId") or p.get("heroId")
        if hero and p.get("isRadiant") is not None:
            result.append(PickBan(int(hero), bool(p["isPick"]), bool(p["isRadiant"]), p["order"]))
    return result or None


def match_from_opendota(row: dict) -> ProMatch:
    """From a ``/proMatches``, ``/leagues/{id}/matches`` or explorer row."""
    league_name = row.get("league_name")
    return ProMatch(
        match_id=int(row["match_id"]),
        start_time=int(row["start_time"]),
        duration=row.get("duration"),
        radiant_win=row.get("radiant_win"),
        league_id=row.get("leagueid"),
        league_name=league_name.strip() if league_name else None,
        radiant_team_id=row.get("radiant_team_id"),
        radiant_name=row.get("radiant_name") or row.get("radiant_team_name"),
        dire_team_id=row.get("dire_team_id"),
        dire_name=row.get("dire_name") or row.get("dire_team_name"),
        radiant_score=row.get("radiant_score"),
        dire_score=row.get("dire_score"),
        picks_bans=_opendota_picks_bans(row.get("picks_bans")),
    )


def _kills(players: list[dict], radiant: bool) -> int | None:
    side = [p.get("kills") for p in players if p.get("isRadiant") is radiant]
    return sum(side) if side and None not in side else None


def match_from_stratz(raw: dict) -> ProMatch:
    players = raw.get("players") or []
    return ProMatch(
        match_id=int(raw["id"]),
        start_time=int(raw["startDateTime"]),
        duration=raw.get("durationSeconds"),
        radiant_win=raw.get("didRadiantWin"),
        league_id=raw.get("leagueId"),
        radiant_team_id=raw.get("radiantTeamId"),
        radiant_name=(raw.get("radiantTeam") or {}).get("name"),
        dire_team_id=raw.get("direTeamId"),
        dire_name=(raw.get("direTeam") or {}).get("name"),
        radiant_score=_kills(players, True),
        dire_score=_kills(players, False),
        picks_bans=_stratz_picks_bans(raw.get("pickBans")),
        radiant_heroes=[p["heroId"] for p in players if p.get("isRadiant") and p.get("heroId")],
        dire_heroes=[
            p["heroId"] for p in players if p.get("isRadiant") is False and p.get("heroId")
        ],
    )


# Fetchers


@cached(ttl=TTL_REFERENCE)
async def fetch_teams() -> list[dict]:
    """OpenDota ``/teams``: about 1,000 teams ordered by rating."""
    return await get_opendota()._get("/teams")


@cached(ttl=TTL_REFERENCE)
async def fetch_leagues() -> list[dict]:
    """OpenDota ``/leagues``: every league ticket, without dates."""
    return await get_opendota()._get("/leagues")


@cached(ttl=TTL_REFERENCE)
async def fetch_pro_players() -> list[dict]:
    """OpenDota ``/proPlayers``: accounts with a registered pro name and team."""
    return await get_opendota()._get("/proPlayers")


@cached(ttl=TTL_PRO_LIST)
async def fetch_recent_pro_matches() -> list[dict]:
    """OpenDota ``/proMatches``: the 100 newest league matches."""
    return await get_opendota().pro_matches()


@cached(ttl=TTL_LIVE)
async def fetch_live() -> list[dict]:
    """OpenDota ``/live``: about 100 currently live top games."""
    return await get_opendota().live()


@cached(ttl=TTL_AGGREGATE)
async def fetch_public_hero_stats() -> list[dict]:
    """OpenDota ``/heroStats`` (``pub_pick``/``pub_win`` over the last 7 days)."""
    return await get_opendota().hero_stats()


@cached(ttl=TTL_AGGREGATE)
async def fetch_hero_pro_matches(hero_id: int) -> list[dict]:
    """OpenDota ``/heroes/{id}/matches``: the 100 newest league matches with the hero."""
    return await get_opendota()._get(f"/heroes/{int(hero_id)}/matches")


async def _explorer(sql: str) -> list[dict]:
    body = await get_opendota().explorer(sql)
    if body.get("err"):
        raise ExplorerError(f"OpenDota explorer failed: {body['err']}")
    return body.get("rows") or []


_MATCH_COLUMNS = (
    "m.match_id, m.start_time, m.duration, m.radiant_win, m.leagueid, l.name AS league_name, "
    "m.radiant_team_id, rt.name AS radiant_name, m.dire_team_id, dt.name AS dire_name, "
    "m.radiant_score, m.dire_score, m.picks_bans"
)
_MATCH_JOINS = (
    "FROM matches m LEFT JOIN leagues l ON l.leagueid = m.leagueid "
    "LEFT JOIN teams rt ON rt.team_id = m.radiant_team_id "
    "LEFT JOIN teams dt ON dt.team_id = m.dire_team_id"
)


def league_matches_sql(league_id: int) -> str:
    return (
        f"SELECT {_MATCH_COLUMNS} {_MATCH_JOINS} WHERE m.leagueid = {int(league_id)} "
        f"ORDER BY m.match_id DESC LIMIT {EXPLORER_LIMIT}"
    )


def head_to_head_sql(team_a: int, team_b: int, limit: int) -> str:
    a, b = int(team_a), int(team_b)
    return (
        f"SELECT {_MATCH_COLUMNS} {_MATCH_JOINS} WHERE (m.radiant_team_id = {a} AND "
        f"m.dire_team_id = {b}) OR (m.radiant_team_id = {b} AND m.dire_team_id = {a}) "
        f"ORDER BY m.match_id DESC LIMIT {int(limit)}"
    )


@cached(ttl=TTL_AGGREGATE)
async def fetch_league_matches(league_id: int) -> list[ProMatch]:
    """Every match of a league with its draft (OpenDota explorer, one call)."""
    return [match_from_opendota(r) for r in await _explorer(league_matches_sql(league_id))]


@cached(ttl=TTL_AGGREGATE)
async def fetch_head_to_head(team_a: int, team_b: int, limit: int = 100) -> list[ProMatch]:
    """Newest league matches between two teams with drafts (OpenDota explorer, one call)."""
    rows = await _explorer(head_to_head_sql(team_a, team_b, limit))
    return [match_from_opendota(r) for r in rows]


_DRAFT_FIELDS = "id pickBans { isPick heroId bannedHeroId order isRadiant }"

TEAM_MATCHES_QUERY = """
query ProTeamMatches($teamId: Int!, $since: Long, $take: Int!, $skip: Int!) {
  team(teamId: $teamId) {
    id name tag
    matches(request: {startDateTime: $since, take: $take, skip: $skip}) {
      id startDateTime durationSeconds didRadiantWin leagueId
      radiantTeamId direTeamId radiantTeam { name } direTeam { name }
      pickBans { isPick heroId bannedHeroId order isRadiant }
      players { isRadiant heroId kills }
    }
  }
}
"""

PLAYER_MATCHES_QUERY = """
query ProPlayerMatches($id: Long!, $take: Int!) {
  player(steamAccountId: $id) {
    steamAccountId
    steamAccount { name proSteamAccount { name realName position } }
    team { team { id name tag } }
    matches(request: {lobbyTypeIds: [1], take: $take}) {
      id startDateTime durationSeconds leagueId
      players(steamAccountId: $id) {
        heroId position isVictory kills deaths assists goldPerMinute experiencePerMinute
      }
    }
  }
}
"""

PUBLIC_BUILDS_QUERY = """
query PublicHeroBuilds($heroId: Short!) {
  heroStats {
    stats(heroIds: [$heroId]) { matchCount winCount }
    itemFullPurchase(heroId: $heroId) { itemId instance time matchCount winCount }
    itemBootPurchase(heroId: $heroId) { itemId instance time matchCount }
    abilityMaxLevel(heroId: $heroId) { abilityId level matchCount winCount }
  }
  constants {
    hero(id: $heroId) { abilities { abilityId ability { name language { displayName } } } }
  }
}
"""


def drafts_query(match_ids: Sequence[int]) -> str:
    fields = " ".join(f"m{int(i)}: match(id: {int(i)}) {{ {_DRAFT_FIELDS} }}" for i in match_ids)
    return f"query ProDrafts {{ {fields} }}"


def hero_builds_query(pairs: Sequence[tuple[int, int]]) -> str:
    """Aliased match fields with one player's purchases and skill order per (match, account)."""
    fields = " ".join(
        f"m{int(m)}: match(id: {int(m)}) {{ id startDateTime players(steamAccountId: {int(a)}) "
        "{ heroId isVictory position abilities { abilityId level isTalent } "
        "stats { itemPurchases { itemId time } } } }"
        for m, a in pairs
    )
    return f"query ProHeroBuilds {{ {fields} }}"


def _batches[T](values: Sequence[T], size: int = STRATZ_BATCH) -> Iterable[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


async def _aliased_matches(
    match_ids: Sequence[int], build: Callable[[Sequence[int]], str]
) -> dict[int, dict | None]:
    """Run ``build(batch)`` per batch of match ids and return each ``m<id>`` field.

    STRATZ fails the whole document when one match times out, so a failed batch is retried
    one match at a time and matches that still fail come back as None.
    """
    found: dict[int, dict | None] = {}
    for batch in _batches(match_ids):
        try:
            data = await get_stratz().query(build(batch))
        except StratzQueryError:
            data = {}
            for match_id in batch:
                try:
                    data |= await get_stratz().query(build([match_id]))
                except StratzQueryError:
                    data[f"m{match_id}"] = None
        for match_id in batch:
            found[match_id] = data.get(f"m{match_id}")
    return found


@cached(ttl=TTL_MATCH)
async def fetch_drafts(match_ids: tuple[int, ...]) -> dict[int, list[PickBan] | None]:
    """Pick/ban order per match from STRATZ (None for matches without a draft)."""
    found = await _aliased_matches(match_ids, drafts_query)
    return {i: _stratz_picks_bans((raw or {}).get("pickBans")) for i, raw in found.items()}


@cached(ttl=TTL_AGGREGATE)
async def fetch_team_matches(team_id: int, since: int) -> tuple[dict | None, list[ProMatch]]:
    """STRATZ team record and its matches started at or after ``since`` (unix seconds)."""
    team = None
    matches: list[ProMatch] = []
    for skip in range(0, MAX_TEAM_MATCHES, STRATZ_PAGE):
        variables = {"teamId": int(team_id), "since": since, "take": STRATZ_PAGE, "skip": skip}
        data = await get_stratz().query(TEAM_MATCHES_QUERY, variables)
        team = data.get("team")
        if not team:
            break
        page = team.get("matches") or []
        matches.extend(match_from_stratz(m) for m in page)
        if len(page) < STRATZ_PAGE:
            break
    info = {k: v for k, v in team.items() if k != "matches"} if team else None
    return info, matches


@cached(ttl=TTL_AGGREGATE)
async def fetch_player_matches(account_id: int, take: int) -> dict | None:
    """STRATZ player record with its newest tournament-lobby matches."""
    data = await get_stratz().query(PLAYER_MATCHES_QUERY, {"id": int(account_id), "take": take})
    return data.get("player")


@cached(ttl=TTL_MATCH)
async def fetch_hero_builds(pairs: tuple[tuple[int, int], ...]) -> list[dict]:
    """Item purchases and skill order for (match_id, account_id) pairs from STRATZ."""
    account = dict(pairs)

    def build(match_ids: Sequence[int]) -> str:
        return hero_builds_query([(m, account[m]) for m in match_ids])

    found = await _aliased_matches([m for m, _ in pairs], build)
    rows = []
    for match_id, raw in found.items():
        if raw and raw.get("players"):
            player = raw["players"][0]
            rows.append({"match_id": match_id, "start_time": raw.get("startDateTime"), **player})
    return rows


@cached(ttl=TTL_AGGREGATE)
async def fetch_public_builds(hero_id: int) -> dict:
    """STRATZ public item purchases and ability max levels for a hero."""
    return await get_stratz().query(PUBLIC_BUILDS_QUERY, {"heroId": int(hero_id)})


async def league_names() -> dict[int, str]:
    return {lg["leagueid"]: (lg.get("name") or "").strip() for lg in await fetch_leagues()}


async def team_names() -> dict[int, str]:
    return {t["team_id"]: t.get("name") for t in await fetch_teams() if t.get("name")}


async def fill_names(matches: Iterable[ProMatch]) -> None:
    """Fill missing league and team names from OpenDota's lists."""
    leagues, teams = await league_names(), await team_names()
    for m in matches:
        if not m.league_name and m.league_id:
            m.league_name = leagues.get(m.league_id) or None
        if not m.radiant_name and m.radiant_team_id:
            m.radiant_name = teams.get(m.radiant_team_id)
        if not m.dire_name and m.dire_team_id:
            m.dire_name = teams.get(m.dire_team_id)


# Formatting helpers


def hero_name(hero_id: int) -> str:
    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"hero {hero_id}"


def item_name(item_id: int) -> str:
    item = item_by_id().get(item_id)
    return (item.display_name or item.name) if item else f"item {item_id}"


def utc(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def date_range(matches: Sequence[ProMatch]) -> tuple[date, date] | None:
    if not matches:
        return None
    times = [m.start_time for m in matches]
    return utc(min(times)).date(), utc(max(times)).date()


def latest_end(matches: Sequence[ProMatch]) -> datetime | None:
    if not matches:
        return None
    return utc(max(m.start_time + (m.duration or 0) for m in matches))


def rate(wins: int, games: int) -> float | None:
    return round(wins / games, 4) if games else None


def ci(wins: int, games: int) -> tuple[float, float]:
    low, high = wilson_interval(wins, games)
    return round(low, 4), round(high, 4)


def minutes(seconds: float | None) -> float | None:
    return round(seconds / 60, 1) if seconds is not None else None


def draft_summary(m: ProMatch) -> dict | None:
    if not m.picks_bans and not (m.radiant_heroes or m.dire_heroes):
        return None
    return {
        "radiant_picks": [hero_name(h) for h in m.heroes(True)],
        "dire_picks": [hero_name(h) for h in m.heroes(False)],
        "radiant_bans": [hero_name(h) for h in m.bans(True)],
        "dire_bans": [hero_name(h) for h in m.bans(False)],
        "first_pick": (
            None
            if m.first_pick_radiant() is None
            else "radiant"
            if m.first_pick_radiant()
            else "dire"
        ),
    }


def match_summary(m: ProMatch, include_draft: bool = True) -> dict:
    winner = None
    if m.radiant_win is not None:
        winner = (m.radiant_name or "radiant") if m.radiant_win else (m.dire_name or "dire")
    summary = {
        "match_id": m.match_id,
        "start_time": utc(m.start_time).isoformat(),
        "league": m.league_name,
        "league_id": m.league_id,
        "radiant": {"team": m.radiant_name, "team_id": m.radiant_team_id, "score": m.radiant_score},
        "dire": {"team": m.dire_name, "team_id": m.dire_team_id, "score": m.dire_score},
        "winner": winner,
        "duration_min": minutes(m.duration),
    }
    if include_draft:
        summary["draft"] = draft_summary(m)
    return summary


# Analyses


def team_stats(team_id: int, matches: Sequence[ProMatch], top: int = 10) -> dict:
    """Results, hero pool and draft tendencies of one team over ``matches``."""
    played = [m for m in matches if m.side_of(team_id) is not None and m.radiant_win is not None]
    wins = sum(1 for m in played if m.radiant_win == m.side_of(team_id))
    durations = [m.duration for m in played if m.duration]
    picks: Counter[int] = Counter()
    pick_wins: Counter[int] = Counter()
    bans_by: Counter[int] = Counter()
    bans_against: Counter[int] = Counter()
    first_picks = drafted = 0
    side_games = {True: 0, False: 0}
    side_wins = {True: 0, False: 0}
    for m in played:
        radiant = bool(m.side_of(team_id))
        won = m.radiant_win == radiant
        side_games[radiant] += 1
        side_wins[radiant] += won
        for hero in m.heroes(radiant):
            picks[hero] += 1
            pick_wins[hero] += won
        if m.picks_bans:
            bans_by.update(m.bans(radiant))
            bans_against.update(m.bans(not radiant))
            first = m.first_pick_radiant()
            if first is not None:
                drafted += 1
                first_picks += first == radiant
    return {
        "matches": len(played),
        "wins": wins,
        "losses": len(played) - wins,
        "win_rate": rate(wins, len(played)),
        "win_rate_ci": ci(wins, len(played)),
        "radiant": {
            "matches": side_games[True],
            "win_rate": rate(side_wins[True], side_games[True]),
        },
        "dire": {
            "matches": side_games[False],
            "win_rate": rate(side_wins[False], side_games[False]),
        },
        "avg_duration_min": minutes(mean(durations)) if durations else None,
        "avg_duration_ci": (
            tuple(minutes(x) for x in bootstrap_ci(durations, stat=mean, seed=0))
            if durations
            else None
        ),
        "hero_pool_size": len(picks),
        "most_picked": [
            {
                "hero": hero_name(h),
                "games": n,
                "wins": pick_wins[h],
                "win_rate": rate(pick_wins[h], n),
            }
            for h, n in picks.most_common(top)
        ],
        "drafts_with_order": drafted,
        "first_pick_share": rate(first_picks, drafted),
        "most_banned_by_team": [
            {"hero": hero_name(h), "bans": n} for h, n in bans_by.most_common(top)
        ],
        "most_banned_against_team": [
            {"hero": hero_name(h), "bans": n} for h, n in bans_against.most_common(top)
        ],
    }


def head_to_head(team_a: int, team_b: int, matches: Sequence[ProMatch], shown: int) -> dict:
    """Record between two teams and the drafts of their newest meetings."""
    played = [
        m
        for m in matches
        if m.side_of(team_a) is not None
        and m.side_of(team_b) is not None
        and m.radiant_win is not None
    ]
    wins_a = sum(1 for m in played if m.radiant_win == m.side_of(team_a))
    heroes: dict[int, Counter[int]] = {team_a: Counter(), team_b: Counter()}
    by_league: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for m in played:
        for team in (team_a, team_b):
            heroes[team].update(m.heroes(bool(m.side_of(team))))
        record = by_league[m.league_name or f"league {m.league_id}"]
        record[0] += 1
        record[1] += m.radiant_win == m.side_of(team_a)
    newest = sorted(played, key=lambda m: -m.start_time)[:shown]
    return {
        "meetings": len(played),
        "team_a_wins": wins_a,
        "team_b_wins": len(played) - wins_a,
        "team_a_win_rate": rate(wins_a, len(played)),
        "team_a_win_rate_ci": ci(wins_a, len(played)),
        "by_league": [
            {"league": name, "meetings": n, "team_a_wins": w}
            for name, (n, w) in sorted(by_league.items(), key=lambda kv: -kv[1][0])
        ],
        "team_a_most_picked": [
            {"hero": hero_name(h), "games": n} for h, n in heroes[team_a].most_common(5)
        ],
        "team_b_most_picked": [
            {"hero": hero_name(h), "games": n} for h, n in heroes[team_b].most_common(5)
        ],
        "recent_meetings": [match_summary(m) for m in newest],
    }


def _record(m: ProMatch, value: float | int | None, unit: str) -> dict:
    return {
        "match_id": m.match_id,
        "value": value,
        "unit": unit,
        "radiant": m.radiant_name,
        "dire": m.dire_name,
        "winner": (m.radiant_name if m.radiant_win else m.dire_name),
        "start_time": utc(m.start_time).isoformat(),
    }


def tournament_stats(
    matches: Sequence[ProMatch], public: Sequence[dict], min_picks: int, top: int = 15
) -> dict:
    """Picks, bans, contest rate and win rates per hero in an event, plus match records."""
    played = [m for m in matches if m.radiant_win is not None]
    drafted = [m for m in played if m.picks_bans]
    picks: Counter[int] = Counter()
    wins: Counter[int] = Counter()
    bans: Counter[int] = Counter()
    for m in drafted:
        for p in m.picks_bans or ():
            if p.is_pick:
                picks[p.hero_id] += 1
                wins[p.hero_id] += p.radiant == m.radiant_win
            else:
                bans[p.hero_id] += 1
    public_rate = {h["id"]: h["pub_win"] / h["pub_pick"] for h in public if h.get("pub_pick")}
    n = len(drafted)
    heroes = []
    for hero in set(picks) | set(bans):
        games = picks[hero]
        pub = public_rate.get(hero)
        win_rate = rate(wins[hero], games)
        heroes.append(
            {
                "hero": hero_name(hero),
                "picks": games,
                "bans": bans[hero],
                "contest_rate": rate(games + bans[hero], n),
                "pick_rate": rate(games, n),
                "ban_rate": rate(bans[hero], n),
                "wins": wins[hero],
                "win_rate": win_rate,
                "win_rate_ci": ci(wins[hero], games) if games else None,
                "public_win_rate": round(pub, 4) if pub is not None else None,
                "vs_public": round(win_rate - pub, 4)
                if win_rate is not None and pub is not None
                else None,
            }
        )
    heroes.sort(key=lambda h: (-(h["contest_rate"] or 0), h["hero"]))
    with_sample = [h for h in heroes if h["picks"] >= min_picks]
    durations = [m.duration for m in played if m.duration]
    timed = [m for m in played if m.duration]
    scored = [m for m in played if m.radiant_score is not None and m.dire_score is not None]
    radiant_wins = sum(1 for m in played if m.radiant_win)
    return {
        "matches": len(played),
        "matches_with_draft": n,
        "radiant_win_rate": rate(radiant_wins, len(played)),
        "radiant_win_rate_ci": ci(radiant_wins, len(played)),
        "avg_duration_min": minutes(mean(durations)) if durations else None,
        "heroes_picked": len(picks),
        "heroes_contested": len(heroes),
        "most_contested": heroes[:top],
        "highest_win_rate": sorted(with_sample, key=lambda h: -h["win_rate"])[: top // 3 or 1],
        "lowest_win_rate": sorted(with_sample, key=lambda h: h["win_rate"])[: top // 3 or 1],
        "records": {
            "longest_match": _record(
                max(timed, key=lambda m: m.duration), minutes(max(durations)), "minutes"
            )
            if timed
            else None,
            "shortest_match": _record(
                min(timed, key=lambda m: m.duration), minutes(min(durations)), "minutes"
            )
            if timed
            else None,
            "most_kills": (
                _record(
                    best := max(scored, key=lambda m: m.radiant_score + m.dire_score),
                    best.radiant_score + best.dire_score,
                    "kills (both teams)",
                )
                if scored
                else None
            ),
        },
    }


POSITIONS = {f"POSITION_{i}": i for i in range(1, 6)}


def pro_player_stats(player: dict, top: int = 10) -> dict:
    """Signature heroes and per-position results from STRATZ tournament-lobby matches."""
    rows = []
    for m in player.get("matches") or []:
        if not m.get("leagueId") or not m.get("players"):
            continue
        p = m["players"][0]
        if p.get("isVictory") is None:
            continue
        rows.append({**p, "start_time": m["startDateTime"], "match_id": m["id"]})
    games = len(rows)
    wins = sum(1 for r in rows if r["isVictory"])
    by_hero: dict[int, list[dict]] = defaultdict(list)
    by_pos: dict[int | None, list[dict]] = defaultdict(list)
    for r in rows:
        by_hero[r["heroId"]].append(r)
        by_pos[POSITIONS.get(r.get("position"))].append(r)

    def kda(rs: list[dict]) -> float | None:
        deaths = sum(r.get("deaths") or 0 for r in rs)
        ka = sum((r.get("kills") or 0) + (r.get("assists") or 0) for r in rs)
        return round(ka / max(deaths, 1), 2) if rs else None

    def block(rs: list[dict]) -> dict:
        w = sum(1 for r in rs if r["isVictory"])
        return {
            "games": len(rs),
            "wins": w,
            "win_rate": rate(w, len(rs)),
            "win_rate_ci": ci(w, len(rs)),
            "kda": kda(rs),
            "avg_gpm": round(mean(r["goldPerMinute"] for r in rs if r.get("goldPerMinute")), 1)
            if any(r.get("goldPerMinute") for r in rs)
            else None,
        }

    heroes = sorted(by_hero.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {
        "matches": games,
        "wins": wins,
        "win_rate": rate(wins, games),
        "win_rate_ci": ci(wins, games),
        "kda": kda(rows),
        "first_match": utc(min(r["start_time"] for r in rows)).isoformat() if rows else None,
        "last_match": utc(max(r["start_time"] for r in rows)).isoformat() if rows else None,
        "signature_heroes": [
            {"hero": hero_name(h), "share": rate(len(rs), games), **block(rs)}
            for h, rs in heroes[:top]
        ],
        "by_position": [
            {"position": pos, "share": rate(len(rs), games), **block(rs)}
            for pos, rs in sorted(by_pos.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ],
        "hero_pool_size": len(by_hero),
    }


def _weighted_median(pairs: Iterable[tuple[float, int]]) -> float | None:
    ordered = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in ordered)
    if not total:
        return None
    seen = 0
    for value, weight in ordered:
        seen += weight
        if seen * 2 >= total:
            return value
    return ordered[-1][0]  # pragma: no cover


def _build_item(item_id: int) -> bool:
    item = item_by_id().get(item_id)
    return bool(
        item
        and not item.is_recipe
        and (item.cost or 0) >= MIN_BUILD_ITEM_COST
        and item.quality != "consumable"
    )


def pro_builds(rows: Sequence[dict], public: dict, top: int = 12) -> dict:
    """Pro item and skill builds on a hero against STRATZ public aggregates."""
    hero_info = ((public.get("constants") or {}).get("hero") or {}).get("abilities") or []
    ability_names = {
        a["abilityId"]: ((a.get("ability") or {}).get("language") or {}).get("displayName")
        or (a.get("ability") or {}).get("name")
        for a in hero_info
        if a.get("ability") and (a["ability"].get("name") or "") != "generic_hidden"
    }
    hero_stats = public.get("heroStats") or {}
    public_matches = sum(s.get("matchCount") or 0 for s in hero_stats.get("stats") or [])

    # Items: share of games where the item was bought at least once, and when.
    n = len(rows)
    pro_items: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for r in rows:
        first: dict[int, int] = {}
        for p in (r.get("stats") or {}).get("itemPurchases") or []:
            first.setdefault(p["itemId"], p["time"])
        for item_id, t in first.items():
            if _build_item(item_id):
                pro_items[item_id].append((t / 60, bool(r.get("isVictory"))))
    # STRATZ purchase counts are not unique matches and cover a different window than
    # heroStats.stats, so public items are compared by popularity rank, not by share.
    public_items: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for row in hero_stats.get("itemFullPurchase") or []:
        if row.get("instance") in (0, None) and _build_item(row["itemId"]):
            public_items[row["itemId"]].append((row["time"], row["matchCount"]))
    public_time: dict[int, float | None] = {k: _weighted_median(v) for k, v in public_items.items()}
    for row in hero_stats.get("itemBootPurchase") or []:
        # Boot rows carry the summed purchase time in seconds instead of minute buckets.
        if row.get("instance") in (0, None) and _build_item(row["itemId"]) and row["matchCount"]:
            public_items[row["itemId"]] = [(0, row["matchCount"])]
            public_time[row["itemId"]] = round(row["time"] / row["matchCount"] / 60, 1)
    public_count = {k: sum(c for _, c in v) for k, v in public_items.items()}
    public_rank = {
        k: i for i, k in enumerate(sorted(public_count, key=lambda k: -public_count[k]), 1)
    }
    pro_rank = {k: i for i, k in enumerate(sorted(pro_items, key=lambda k: -len(pro_items[k])), 1)}

    items = []
    for item_id, buys in pro_items.items():
        items.append(
            {
                "item": item_name(item_id),
                "pro_games": len(buys),
                "pro_share": rate(len(buys), n),
                "pro_rank": pro_rank[item_id],
                "pro_median_min": round(median(t for t, _ in buys), 1),
                "pro_win_rate_when_bought": rate(sum(w for _, w in buys), len(buys)),
                "public_purchases": public_count.get(item_id),
                "public_rank": public_rank.get(item_id),
                "public_typical_min": public_time.get(item_id),
            }
        )
    items.sort(key=lambda i: (-i["pro_games"], i["item"]))
    public_only = sorted(
        (k for k in public_count if k not in pro_items), key=lambda k: public_rank[k]
    )

    # Skills: first ten skill points, and the point at which each ability was maxed.
    sequences: Counter[tuple[str, ...]] = Counter()
    final_counts: dict[int, Counter[int]] = defaultdict(Counter)
    orders = []
    for r in rows:
        order = [a["abilityId"] for a in r.get("abilities") or [] if not a.get("isTalent")]
        order = [a for a in order if a in ability_names]
        if not order:
            continue
        orders.append(order)
        sequences[tuple(ability_names[a] for a in order[:10])] += 1
        for ability, count in Counter(order).items():
            final_counts[ability][count] += 1
    max_points = {a: max(c, key=lambda k: (c[k], k)) for a, c in final_counts.items()}
    maxed_at: dict[int, list[int]] = defaultdict(list)
    for order in orders:
        seen: Counter[int] = Counter()
        for index, ability in enumerate(order, start=1):
            seen[ability] += 1
            if seen[ability] == max_points[ability]:
                maxed_at[ability].append(index)
    public_max: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for row in hero_stats.get("abilityMaxLevel") or []:
        if row.get("abilityId") in ability_names:
            public_max[row["abilityId"]].append((row["level"], row["matchCount"]))
    abilities = []
    for ability, name in ability_names.items():
        pro_level = round(median(maxed_at[ability]), 1) if maxed_at.get(ability) else None
        abilities.append(
            {
                "ability": name,
                "pro_median_maxed_at": pro_level,
                "pro_games_maxed": len(maxed_at.get(ability, ())),
                "public_median_maxed_level": _weighted_median(public_max.get(ability, ())),
            }
        )

    def priority(key: str) -> list[str]:
        ranked = [a for a in abilities if a[key] is not None]
        return [a["ability"] for a in sorted(ranked, key=lambda a: a[key])]

    return {
        "pro_games": n,
        "public_games": public_matches,
        "items": items[:top],
        "public_items_pros_skip": [
            {
                "item": item_name(k),
                "public_rank": public_rank[k],
                "public_purchases": public_count[k],
            }
            for k in public_only[:5]
        ],
        "skills": {
            "pro_common_first_10": [
                {"order": list(seq), "games": c} for seq, c in sequences.most_common(3)
            ],
            "abilities": abilities,
            "pro_max_priority": priority("pro_median_maxed_at"),
            "public_max_priority": priority("public_median_maxed_level"),
        },
    }


def live_games(games: Sequence[dict], leagues: dict[int, str], limit: int, pro_only: bool) -> list:
    """Top live games, league games first, then by OpenDota's ``sort_score``.

    Games with a ``deactivate_time`` have ended and are left out.
    """
    selected = [
        g
        for g in games
        if not g.get("deactivate_time")
        and (
            not pro_only or g.get("league_id") or any(p.get("is_pro") for p in g.get("players", []))
        )
    ]
    selected.sort(key=lambda g: (not g.get("league_id"), -(g.get("sort_score") or 0)))
    result = []
    for g in selected[:limit]:
        players = [
            {
                "name": p.get("name"),
                "pro_team": p.get("team_name"),
                "hero": hero_name(p["hero_id"]) if p.get("hero_id") else None,
                "side": "radiant" if p.get("team") == 0 else "dire",
            }
            for p in g.get("players", [])
        ]
        game_time = g.get("game_time") or 0
        result.append(
            {
                "match_id": int(g["match_id"]),
                "league": leagues.get(g.get("league_id")) if g.get("league_id") else None,
                "league_id": g.get("league_id") or None,
                "radiant_team": g.get("team_name_radiant") or None,
                "dire_team": g.get("team_name_dire") or None,
                "game_time": f"{game_time // 60}:{game_time % 60:02d}" if game_time >= 0 else None,
                "game_time_s": game_time,
                "radiant_score": g.get("radiant_score"),
                "dire_score": g.get("dire_score"),
                "radiant_networth_lead": g.get("radiant_lead"),
                "average_mmr": g.get("average_mmr") or None,
                "spectators": g.get("spectators"),
                "delay_s": g.get("delay"),
                "last_update": utc(g["last_update_time"]).isoformat()
                if g.get("last_update_time")
                else None,
                "players": players,
            }
        )
    return result
