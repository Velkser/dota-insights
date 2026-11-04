"""Economy analytics: cost of dying, farm distribution, comebacks and objective impact.

Sources:

- OpenDota ``/explorer`` (SQL) over parsed league matches (pro and amateur leagues): the
  match-level ``radiant_gold_adv`` per-minute array and ``objectives`` (towers, barracks,
  Roshan, Tormentor), and per-player ``net_worth``, ``gold_t``, ``lane_role``, ``kills_log``,
  ``buyback_log``, ``gold_reasons`` (death gold), ``life_state`` (seconds dead) and ``runes``.
  The explorer's ``matches`` table holds league matches only, so these numbers describe
  league play, not ranked pubs. Every tool shares one recent sample of matches.
- STRATZ GraphQL ``heroStats.stats``: per-hero ``comeBackWon`` / ``comeBackLost`` shares in
  ranked pubs, by medal pair and position.
"""

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import mean, median
from typing import Any

from pydantic import BaseModel, Field

from dota_insights.analytics.farming import (
    fetch_explorer,
    fetch_stratz,
    stratz_basic_brackets,
    stratz_position,
)
from dota_insights.analytics.stats import (
    bootstrap_ci,
    smoothed_rate,
    two_proportion_ztest,
    wilson_interval,
)
from dota_insights.models import Filters
from dota_insights.reference import hero_by_id, heroes

# Newest parsed league matches on the patch that every economy tool works from.
SAMPLE_MATCHES = 500
# Hero-specific death analysis: newest matches with the hero.
HERO_SAMPLE_MATCHES = 300
HERO_PREFIX = "npc_dota_hero_"
BOOTSTRAP_SEED = 7
BOOTSTRAP_RESAMPLES = 1000
ALPHA = 0.05


def _sample_sub(patch: str, hero_id: int | None, limit: int) -> str:
    """Match ids of the newest parsed league matches on ``patch`` (with ``hero_id``)."""
    if hero_id is None:
        return (
            "SELECT m.match_id FROM matches m JOIN match_patch mp USING (match_id) "
            f"WHERE mp.patch = '{patch}' AND m.radiant_gold_adv IS NOT NULL "
            f"ORDER BY m.match_id DESC LIMIT {int(limit)}"
        )
    return (
        "SELECT p.match_id FROM player_matches p JOIN matches m USING (match_id) "
        "JOIN match_patch mp USING (match_id) "
        f"WHERE p.hero_id = {int(hero_id)} AND mp.patch = '{patch}' "
        f"AND m.radiant_gold_adv IS NOT NULL ORDER BY p.match_id DESC LIMIT {int(limit)}"
    )


def match_sql(patch: str, hero_id: int | None = None, limit: int = SAMPLE_MATCHES) -> str:
    """Per match: result, team gold advantage per minute and compact objective events.

    Each event is ``"<second>:<kind>:<team>"`` where kind is roshan, tormentor, tower or
    barracks and team is the side that took it (2 Radiant, 3 Dire). A destroyed
    ``goodguys`` (Radiant) building counts for Dire, also when creeps or a deny finished it.
    """
    return (
        "SELECT m.match_id, m.start_time, m.duration, m.radiant_win, m.radiant_gold_adv, "
        "ARRAY(SELECT concat_ws(':', o->>'time', CASE "
        "WHEN o->>'type' = 'CHAT_MESSAGE_ROSHAN_KILL' THEN 'roshan:' || (o->>'team') "
        "WHEN o->>'type' = 'CHAT_MESSAGE_MINIBOSS_KILL' THEN 'tormentor:' || (o->>'team') "
        "WHEN o->>'key' LIKE '%tower%' THEN 'tower:' || "
        "CASE WHEN o->>'key' LIKE '%goodguys%' THEN '3' ELSE '2' END "
        "WHEN o->>'key' LIKE '%rax%' THEN 'barracks:' || "
        "CASE WHEN o->>'key' LIKE '%goodguys%' THEN '3' ELSE '2' END END) "
        "FROM unnest(m.objectives) o "
        "WHERE o->>'type' IN ('CHAT_MESSAGE_ROSHAN_KILL', 'CHAT_MESSAGE_MINIBOSS_KILL') "
        "OR (o->>'type' = 'building_kill' AND (o->>'key' LIKE '%tower%' "
        "OR o->>'key' LIKE '%rax%'))) AS events "
        f"FROM matches m WHERE m.match_id IN ({_sample_sub(patch, hero_id, limit)})"
    )


def player_sql(patch: str, hero_id: int | None = None, limit: int = SAMPLE_MATCHES) -> str:
    """Per player of the same matches: farm, deaths, hero kills, buybacks and runes."""
    return (
        "SELECT pm.match_id, pm.player_slot, pm.hero_id, pm.lane_role, pm.is_roaming, "
        "pm.net_worth, pm.gold_t[11] AS gold_10, pm.gold_t[16] AS gold_15, "
        "pm.gold_t[21] AS gold_20, pm.gold_t[31] AS gold_30, pm.deaths, "
        "-(pm.gold_reasons->>'1')::int AS death_gold, (pm.life_state->>'2')::int AS dead_s, "
        "ARRAY(SELECT concat(k->>'time', ':', replace(k->>'key', 'npc_dota_hero_', '')) "
        "FROM unnest(pm.kills_log) k WHERE k->>'key' LIKE 'npc_dota_hero_%') AS kills, "
        "ARRAY(SELECT (b->>'time')::int FROM unnest(pm.buyback_log) b) AS buybacks, "
        "pm.runes "
        f"FROM player_matches pm WHERE pm.match_id IN ({_sample_sub(patch, hero_id, limit)})"
    )


@dataclass(frozen=True)
class Event:
    time: int  # seconds
    kind: str  # roshan, tormentor, tower, barracks
    radiant: bool  # taken by Radiant


@dataclass(frozen=True)
class Match:
    match_id: int
    start_time: int
    duration: int
    radiant_win: bool
    adv: tuple[int, ...]  # Radiant gold advantage per minute, index = minute
    events: tuple[Event, ...]

    def lead(self, radiant: bool, minute: int) -> int | None:
        """Gold lead of one side at ``minute`` (negative when behind), None past the end."""
        if minute < 0 or minute >= len(self.adv):
            return None
        return self.adv[minute] if radiant else -self.adv[minute]

    def won(self, radiant: bool) -> bool:
        return self.radiant_win == radiant


@dataclass(frozen=True)
class Kill:
    time: int
    victim: str  # internal hero name without the npc_dota_hero_ prefix


@dataclass(frozen=True)
class Player:
    match_id: int
    slot: int
    hero_id: int
    lane_role: int | None
    roaming: bool
    net_worth: int | None
    gold: Mapping[int, int | None]  # minute -> gold_t
    deaths: int
    death_gold: int | None
    dead_s: int | None
    kills: tuple[Kill, ...]
    buybacks: tuple[int, ...]
    runes: Mapping[int, int]

    @property
    def radiant(self) -> bool:
        return self.slot < 128


def parse_events(raw: Iterable[str] | None) -> tuple[Event, ...]:
    out = []
    for text in raw or []:
        parts = (text or "").split(":")
        if len(parts) != 3 or not parts[0].lstrip("-").isdigit() or parts[2] not in ("2", "3"):
            continue
        out.append(Event(time=int(parts[0]), kind=parts[1], radiant=parts[2] == "2"))
    return tuple(sorted(out, key=lambda e: e.time))


def parse_matches(rows: Sequence[Mapping[str, Any]]) -> list[Match]:
    out = []
    for row in rows:
        adv = row.get("radiant_gold_adv")
        if not adv or row.get("radiant_win") is None:
            continue
        out.append(
            Match(
                match_id=row["match_id"],
                start_time=row["start_time"],
                duration=row["duration"] or 0,
                radiant_win=bool(row["radiant_win"]),
                adv=tuple(int(v) for v in adv),
                events=parse_events(row.get("events")),
            )
        )
    return out


def _parse_kills(raw: Iterable[str] | None) -> tuple[Kill, ...]:
    out = []
    for text in raw or []:
        time, _, victim = text.partition(":")
        if time.lstrip("-").isdigit() and victim:
            out.append(Kill(time=int(time), victim=victim.removeprefix(HERO_PREFIX)))
    return tuple(out)


def parse_players(rows: Sequence[Mapping[str, Any]]) -> list[Player]:
    return [
        Player(
            match_id=row["match_id"],
            slot=row["player_slot"],
            hero_id=row["hero_id"],
            lane_role=row.get("lane_role"),
            roaming=bool(row.get("is_roaming")),
            net_worth=row.get("net_worth"),
            gold={m: row.get(f"gold_{m}") for m in FARM_MINUTES},
            deaths=row.get("deaths") or 0,
            death_gold=row.get("death_gold"),
            dead_s=row.get("dead_s"),
            kills=_parse_kills(row.get("kills")),
            buybacks=tuple(sorted(row.get("buybacks") or [])),
            runes={int(k): int(v) for k, v in (row.get("runes") or {}).items()},
        )
        for row in rows
    ]


@dataclass(frozen=True)
class Sample:
    matches: list[Match]
    players: list[Player]
    match_rows: list[dict[str, Any]]

    def by_match(self) -> dict[int, list[Player]]:
        known = {m.match_id for m in self.matches}
        out: dict[int, list[Player]] = defaultdict(list)
        for p in self.players:
            if p.match_id in known:
                out[p.match_id].append(p)
        return out


async def fetch_sample(patch: str, hero_id: int | None = None, limit: int | None = None) -> Sample:
    """The shared match and player sample (two cached explorer queries)."""
    n = limit or (SAMPLE_MATCHES if hero_id is None else HERO_SAMPLE_MATCHES)
    match_rows = await fetch_explorer(match_sql(patch, hero_id, n))
    player_rows = await fetch_explorer(player_sql(patch, hero_id, n))
    return Sample(parse_matches(match_rows), parse_players(player_rows), match_rows)


# Shared result pieces


class Rate(BaseModel):
    games: int
    wins: int
    win_rate: float | None = Field(description="wins / games; null when games is 0.")
    ci_low: float
    ci_high: float


def rate(wins: int, games: int) -> Rate:
    low, high = wilson_interval(wins, games)
    return Rate(
        games=games, wins=wins, win_rate=wins / games if games else None, ci_low=low, ci_high=high
    )


class Comparison(BaseModel):
    """Win rate with and without some condition, among the same situations."""

    name: str
    description: str
    present: Rate = Field(description="Situations where the condition held.")
    absent: Rate = Field(description="Situations where it did not.")
    difference: float | None = Field(description="present.win_rate - absent.win_rate.")
    p_value: float | None = Field(description="Pooled two-proportion z-test, two-sided.")


def compare(name: str, description: str, outcomes: Iterable[tuple[bool, bool]]) -> Comparison:
    """``outcomes`` = (condition, won) pairs."""
    counts = [[0, 0], [0, 0]]  # [condition][wins, games]
    for condition, won in outcomes:
        cell = counts[0 if condition else 1]
        cell[0] += int(won)
        cell[1] += 1
    (w1, n1), (w2, n2) = counts
    diff = p = None
    if n1 and n2:
        diff = w1 / n1 - w2 / n2
        p = two_proportion_ztest(w1, n1, w2, n2).p_value
    return Comparison(
        name=name,
        description=description,
        present=rate(w1, n1),
        absent=rate(w2, n2),
        difference=diff,
        p_value=p,
    )


def _ci_mean(values: Sequence[float]) -> tuple[float, float]:
    if len(values) < 2:
        v = float(values[0]) if values else 0.0
        return v, v
    low, high = bootstrap_ci(values, mean, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED)
    return low, high


def _npc_names() -> dict[str, int]:
    return {h.name.removeprefix(HERO_PREFIX): h.id for h in heroes()}


def _hero_name(hero_id: int) -> str:
    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"hero {hero_id}"


# Comebacks

COMEBACK_GRID_MINUTES = (10, 15, 20, 25, 30, 35, 40)
COMEBACK_GRID_DEFICITS = (2000, 5000, 10000, 15000)
TREND_MINUTES = 5
MIN_HERO_GAMES = 10
HERO_PRIOR_STRENGTH = 20
TOP_HEROES = 10
STRATZ_MIN_HERO_MATCHES = 1000


class ComebackCell(BaseModel):
    minute: int
    deficit: int = Field(description="Behind by at least this much gold at the minute.")
    games: int
    wins: int
    win_rate: float | None


class ComebackHero(BaseModel):
    hero: str
    hero_id: int
    games: int = Field(description="Times the hero's team was behind at the minute.")
    wins: int
    win_rate: float
    smoothed_win_rate: float = Field(description="Shrunk towards the overall comeback rate.")


class PubComebackHero(BaseModel):
    hero: str
    hero_id: int
    matches: int
    comeback_win_share: float = Field(description="STRATZ comeBackWon (see definitions).")
    comeback_loss_share: float = Field(description="STRATZ comeBackLost.")


class ComebackStats(BaseModel):
    patch: str
    minute: int
    deficit: int
    situations: int = Field(description="Matches where one team was behind by >= deficit.")
    matches_reaching_minute: int
    comeback: Rate = Field(description="Win rate of the team that was behind.")
    grid: list[ComebackCell] = Field(description="Comeback rate by minute and deficit.")
    factors: list[Comparison] = Field(
        description="Among the behind teams: win rate with vs without each factor."
    )
    heroes: list[ComebackHero] = Field(description="League heroes that came back most.")
    pub_heroes: list[PubComebackHero] = Field(
        description="Ranked-pub heroes with the highest STRATZ comeback-win share."
    )


def behind_side(match: Match, minute: int, deficit: int) -> bool | None:
    """True if Radiant, False if Dire is behind by >= ``deficit`` at ``minute``, else None."""
    lead = match.lead(True, minute)
    if lead is None:
        return None
    if lead <= -deficit:
        return True
    if lead >= deficit:
        return False
    return None


def comeback_grid(matches: Sequence[Match]) -> list[ComebackCell]:
    cells = []
    for minute in COMEBACK_GRID_MINUTES:
        for deficit in COMEBACK_GRID_DEFICITS:
            games = wins = 0
            for m in matches:
                side = behind_side(m, minute, deficit)
                if side is not None:
                    games += 1
                    wins += m.won(side)
            cells.append(ComebackCell(minute=minute, deficit=deficit, games=games, wins=wins,
                                      win_rate=wins / games if games else None))  # fmt: skip
    return cells


def _first_after(match: Match, kind: str, second: int) -> Event | None:
    return next((e for e in match.events if e.kind == kind and e.time >= second), None)


def comeback_factors(situations: Sequence[tuple[Match, bool]], minute: int) -> list[Comparison]:
    t = minute * 60

    def factor(name: str, description: str, test: Callable[[Match, bool], bool | None]):
        pairs = []
        for m, side in situations:
            value = test(m, side)
            if value is not None:
                pairs.append((value, m.won(side)))
        return compare(name, description, pairs)

    def next_taken(kind: str) -> Callable[[Match, bool], bool | None]:
        def test(m: Match, side: bool) -> bool | None:
            event = _first_after(m, kind, t)
            return None if event is None else event.radiant == side

        return test

    def shrinking(m: Match, side: bool) -> bool | None:
        before = m.lead(side, minute - TREND_MINUTES)
        now = m.lead(side, minute)
        return None if before is None or now is None else now > before

    def barracks_intact(m: Match, side: bool) -> bool:
        return not any(e.kind == "barracks" and e.radiant != side and e.time < t for e in m.events)

    return [
        factor("took_next_roshan", "The behind team killed the first Roshan after the minute.",
               next_taken("roshan")),
        factor("took_next_tower", "The behind team destroyed the first tower after the minute.",
               next_taken("tower")),
        factor("took_next_tormentor",
               "The behind team killed the first Tormentor after the minute.",
               next_taken("tormentor")),
        factor("deficit_shrinking",
               f"The deficit was smaller than {TREND_MINUTES} minutes earlier.", shrinking),
        factor("barracks_intact", "The behind team had lost no barracks by the minute.",
               barracks_intact),
    ]  # fmt: skip


def comeback_heroes(
    situations: Sequence[tuple[Match, bool]], players: Mapping[int, list[Player]], prior: float
) -> list[ComebackHero]:
    table: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for m, side in situations:
        won = m.won(side)
        for p in players.get(m.match_id, []):
            if p.radiant == side:
                table[p.hero_id][0] += int(won)
                table[p.hero_id][1] += 1
    out = [
        ComebackHero(
            hero=_hero_name(hero_id),
            hero_id=hero_id,
            games=games,
            wins=wins,
            win_rate=wins / games,
            smoothed_win_rate=smoothed_rate(wins, games, prior, HERO_PRIOR_STRENGTH),
        )
        for hero_id, (wins, games) in table.items()
        if games >= MIN_HERO_GAMES
    ]
    return sorted(out, key=lambda h: (-h.smoothed_win_rate, -h.games))[:TOP_HEROES]


COMEBACK_DOC = """
query Comeback($brackets: [RankBracketBasicEnum], $positions: [MatchPlayerPositionType]) {
  heroStats {
    stats(bracketBasicIds: $brackets, positionIds: $positions) {
      heroId matchCount comeBackWon comeBackLost
    }
  }
}
"""


def pub_comeback_heroes(data: Mapping[str, Any]) -> list[PubComebackHero]:
    rows = (data.get("heroStats") or {}).get("stats") or []
    out = [
        PubComebackHero(
            hero=_hero_name(r["heroId"]),
            hero_id=r["heroId"],
            matches=r["matchCount"],
            comeback_win_share=r["comeBackWon"],
            comeback_loss_share=r["comeBackLost"] or 0.0,
        )
        for r in rows
        if r.get("comeBackWon") is not None
        and (r.get("matchCount") or 0) >= STRATZ_MIN_HERO_MATCHES
        and r["heroId"] in hero_by_id()
    ]
    return sorted(out, key=lambda h: (-h.comeback_win_share, -h.matches))[:TOP_HEROES]


def comeback_variables(filters: Filters) -> dict[str, Any]:
    values = {
        "brackets": stratz_basic_brackets(filters),
        "positions": stratz_position(filters),
    }
    return {k: v for k, v in values.items() if v is not None}


async def fetch_pub_comebacks(variables: Mapping[str, Any]) -> list[PubComebackHero]:
    return pub_comeback_heroes(await fetch_stratz(COMEBACK_DOC, variables))


def comeback_stats(
    patch: str, sample: Sample, minute: int, deficit: int, pub: list[PubComebackHero]
) -> ComebackStats:
    reaching = [m for m in sample.matches if m.lead(True, minute) is not None]
    situations = []
    for m in reaching:
        side = behind_side(m, minute, deficit)
        if side is not None:
            situations.append((m, side))
    wins = sum(m.won(side) for m, side in situations)
    overall = rate(wins, len(situations))
    prior = overall.win_rate if overall.win_rate is not None else 0.5
    return ComebackStats(
        patch=patch,
        minute=minute,
        deficit=deficit,
        situations=len(situations),
        matches_reaching_minute=len(reaching),
        comeback=overall,
        grid=comeback_grid(sample.matches),
        factors=comeback_factors(situations, minute),
        heroes=comeback_heroes(situations, sample.by_match(), prior),
        pub_heroes=pub,
    )


# Objective impact

# Dota rune types (DOTA_RUNE_*), the keys of OpenDota's per-player ``runes`` counts.
POWER_RUNES = frozenset({0, 1, 2, 3, 4, 6, 9})  # DD, haste, illusion, invis, regen, arcane, shield
BOUNTY_RUNE = 5
WATER_RUNE = 7
WISDOM_RUNE = 8
EVEN_LEAD = 2000


class ObjectiveImpact(BaseModel):
    objective: str
    description: str
    decided: int = Field(description="Matches where one team got it (ties and none excluded).")
    taker: Rate = Field(description="Win rate of the team that got it.")
    lift: float | None = Field(description="taker.win_rate - 0.5.")
    p_value: float | None = Field(description="Two-sided test of taker win rate = 50%.")
    median_lead_at_event: float | None = Field(
        description="Taker's median gold lead at the event minute; null for whole-game counts."
    )
    when_even: Rate | None = Field(
        description="Taker win rate when the lead at the event was within +/- EVEN_LEAD."
    )


class ObjectiveStats(BaseModel):
    patch: str
    matches: int
    objectives: list[ObjectiveImpact]


def _vs_half(wins: int, games: int) -> float | None:
    """Two-sided normal-approximation test of a proportion against 0.5."""
    if not games:
        return None
    z = (wins / games - 0.5) / math.sqrt(0.25 / games)
    return min(1.0, math.erfc(abs(z) / math.sqrt(2)))


def _impact(
    name: str,
    description: str,
    takes: Sequence[tuple[Match, bool, int | None]],
) -> ObjectiveImpact:
    """``takes`` = (match, taker is Radiant, event second or None for whole-game counts)."""
    wins = sum(m.won(side) for m, side, _ in takes)
    n = len(takes)
    leads, even = [], [0, 0]
    for m, side, second in takes:
        if second is None:
            continue
        lead = m.lead(side, max(0, second) // 60)
        if lead is None:
            continue
        leads.append(lead)
        if abs(lead) <= EVEN_LEAD:
            even[0] += int(m.won(side))
            even[1] += 1
    timed = any(second is not None for _, _, second in takes)
    return ObjectiveImpact(
        objective=name,
        description=description,
        decided=n,
        taker=rate(wins, n),
        lift=wins / n - 0.5 if n else None,
        p_value=_vs_half(wins, n),
        median_lead_at_event=median(leads) if leads else None,
        when_even=rate(*even) if timed else None,
    )


def first_blood(players: Sequence[Player], npc: Mapping[str, int]) -> tuple[bool, int] | None:
    """(killer is Radiant, second) of the earliest hero kill by a hero of the other team."""
    sides = {p.hero_id: p.radiant for p in players}
    best = None
    for p in players:
        for k in p.kills:
            victim = sides.get(npc.get(k.victim, -1))
            if victim is None or victim == p.radiant:
                continue
            if best is None or k.time < best[1]:
                best = (p.radiant, k.time)
    return best


def _first(match: Match, kind: str) -> tuple[bool, int] | None:
    event = next((e for e in match.events if e.kind == kind), None)
    return None if event is None else (event.radiant, event.time)


def _rune_leader(players: Sequence[Player], kinds: Iterable[int]) -> bool | None:
    wanted = set(kinds)
    totals = {True: 0, False: 0}
    for p in players:
        totals[p.radiant] += sum(v for k, v in p.runes.items() if k in wanted)
    if totals[True] == totals[False]:
        return None
    return totals[True] > totals[False]


def objective_stats(patch: str, sample: Sample) -> ObjectiveStats:
    players = sample.by_match()
    npc = _npc_names()
    timed: dict[str, list[tuple[Match, bool, int | None]]] = defaultdict(list)
    for m in sample.matches:
        roster = players.get(m.match_id, [])
        fb = first_blood(roster, npc) if roster else None
        if fb:
            timed["first_blood"].append((m, fb[0], fb[1]))
        for kind in ("tower", "roshan", "tormentor", "barracks"):
            got = _first(m, kind)
            if got:
                timed[f"first_{kind}"].append((m, got[0], got[1]))
        roshans = [e.radiant for e in m.events if e.kind == "roshan"]
        if len(roshans) >= 2 and roshans[0] == roshans[1]:
            timed["two_roshans_in_a_row"].append(
                (m, roshans[0], [e.time for e in m.events if e.kind == "roshan"][1])
            )
        if roster:
            for name, kinds in (
                ("more_power_runes", POWER_RUNES),
                ("more_bounty_runes", {BOUNTY_RUNE}),
                ("more_wisdom_runes", {WISDOM_RUNE}),
            ):
                side = _rune_leader(roster, kinds)
                if side is not None:
                    timed[name].append((m, side, None))
    descriptions = {
        "first_blood": "Team that got the first hero kill.",
        "first_tower": "Team that destroyed the first tower.",
        "first_roshan": "Team that killed the first Roshan (and so took the first Aegis).",
        "two_roshans_in_a_row": "Team that killed both the first and the second Roshan.",
        "first_tormentor": "Team that killed the first Tormentor.",
        "first_barracks": "Team that destroyed the first barracks.",
        "more_power_runes": "Team that picked up more power runes over the match.",
        "more_bounty_runes": "Team that picked up more bounty runes over the match.",
        "more_wisdom_runes": "Team that picked up more wisdom runes over the match.",
    }
    return ObjectiveStats(
        patch=patch,
        matches=len(sample.matches),
        objectives=[_impact(name, text, timed[name]) for name, text in descriptions.items()],
    )


# Farm distribution

FARM_MINUTES = (10, 15, 20, 30)
POSITION_ORDER_MINUTE = 10
SHARE_BINS: dict[str, tuple[float, ...]] = {
    "carry_share": (0.0, 0.20, 0.25, 0.30, 0.35),
    "core_share": (0.0, 0.60, 0.65, 0.70, 0.75),
}
MIN_BUCKET_TEAMS = 30


@dataclass(frozen=True)
class TeamFarm:
    shares: tuple[float, float, float, float, float]  # position 1..5
    won: bool


class PositionShare(BaseModel):
    position: int
    share: float = Field(description="Mean share of team gold, all teams.")
    share_in_wins: float | None
    share_in_losses: float | None
    difference: float | None = Field(description="share_in_wins - share_in_losses.")
    ci_low: float
    ci_high: float


class ShareBucket(BaseModel):
    label: str
    from_share: float
    to_share: float | None
    teams: int
    wins: int
    win_rate: float | None
    ci_low: float
    ci_high: float


class FarmDistribution(BaseModel):
    patch: str
    minute: int | None = Field(description="Minute of gold_t used; null for final net worth.")
    teams: int
    skipped_teams: int = Field(
        description="Teams left out: lanes did not form the 2-1-2 pattern or farm was missing."
    )
    positions: list[PositionShare]
    carry_share_buckets: list[ShareBucket]
    core_share_buckets: list[ShareBucket]
    best_carry_share: str | None = Field(
        description=f"carry_share bucket with the highest win rate (>= {MIN_BUCKET_TEAMS} teams)."
    )
    best_core_share: str | None


def assign_positions(team: Sequence[Player]) -> list[Player] | None:
    """Players ordered as positions 1..5, or None when the lanes are not 2 safe/1 mid/2 off.

    Inside the safe lane the player with more gold at minute 10 is position 1 and the other
    position 5; inside the offlane the richer one is position 3, the other position 4.
    """
    if len(team) != 5:
        return None
    lanes: dict[int | None, list[Player]] = defaultdict(list)
    for p in team:
        lanes[p.lane_role].append(p)
    if len(lanes[1]) != 2 or len(lanes[2]) != 1 or len(lanes[3]) != 2:
        return None
    if any(p.gold[POSITION_ORDER_MINUTE] is None for p in team):
        return None

    def rich(p: Player) -> int:
        return p.gold[POSITION_ORDER_MINUTE] or 0

    safe = sorted(lanes[1], key=rich, reverse=True)
    off = sorted(lanes[3], key=rich, reverse=True)
    return [safe[0], lanes[2][0], off[0], off[1], safe[1]]


def team_farms(
    matches: Sequence[Match], players: Mapping[int, list[Player]], minute: int | None
) -> tuple[list[TeamFarm], int]:
    teams, skipped = [], 0
    for m in matches:
        roster = players.get(m.match_id, [])
        for side in (True, False):
            ordered = assign_positions([p for p in roster if p.radiant == side])
            gold = None
            if ordered is not None:
                gold = [p.net_worth if minute is None else p.gold[minute] for p in ordered]
            if gold is None or any(g is None for g in gold) or sum(gold) <= 0:
                skipped += 1
                continue
            total = sum(gold)
            shares = tuple(g / total for g in gold)
            teams.append(TeamFarm(shares=shares, won=m.won(side)))  # type: ignore[arg-type]
    return teams, skipped


def share_buckets(values: Sequence[tuple[float, bool]], edges: Sequence[float]):
    out = []
    for i, lo in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else None
        inside = [won for share, won in values if share >= lo and (hi is None or share < hi)]
        wins, n = sum(inside), len(inside)
        low, high = wilson_interval(wins, n)
        label = f"{lo:.0%}-{hi:.0%}" if hi is not None else f"{lo:.0%}+"
        out.append(ShareBucket(label=label, from_share=lo, to_share=hi, teams=n, wins=wins,
                               win_rate=wins / n if n else None, ci_low=low,
                               ci_high=high))  # fmt: skip
    return out


def _best(buckets: Sequence[ShareBucket]) -> str | None:
    usable = [b for b in buckets if b.teams >= MIN_BUCKET_TEAMS and b.win_rate is not None]
    return max(usable, key=lambda b: b.win_rate or 0).label if usable else None


def farm_distribution(patch: str, sample: Sample, minute: int | None) -> FarmDistribution:
    teams, skipped = team_farms(sample.matches, sample.by_match(), minute)
    positions = []
    for i in range(5):
        all_shares = [t.shares[i] for t in teams]
        wins = [t.shares[i] for t in teams if t.won]
        losses = [t.shares[i] for t in teams if not t.won]
        low, high = _ci_mean(all_shares)
        positions.append(
            PositionShare(
                position=i + 1,
                share=mean(all_shares) if all_shares else 0.0,
                share_in_wins=mean(wins) if wins else None,
                share_in_losses=mean(losses) if losses else None,
                difference=mean(wins) - mean(losses) if wins and losses else None,
                ci_low=low,
                ci_high=high,
            )
        )
    carry = share_buckets([(t.shares[0], t.won) for t in teams], SHARE_BINS["carry_share"])
    core = share_buckets([(sum(t.shares[:3]), t.won) for t in teams], SHARE_BINS["core_share"])
    return FarmDistribution(
        patch=patch,
        minute=minute,
        teams=len(teams),
        skipped_teams=skipped,
        positions=positions,
        carry_share_buckets=carry,
        core_share_buckets=core,
        best_carry_share=_best(carry),
        best_core_share=_best(core),
    )


# Cost of dying

DEATH_BUCKETS = (0, 10, 20, 30, 40)
BUYBACK_FROM_MIN = 20
BUYBACK_LATE_MIN = 35
BUYBACK_LEAD = 5000
# A death counts as bought back if the victim bought back within this many seconds of it
# (and before dying again). Respawn times stay under two minutes in normal play.
BUYBACK_WINDOW_S = 150
DEFAULT_LATER_MINUTES = 3


@dataclass(frozen=True)
class Death:
    match: Match
    victim: Player
    time: int
    bought_back: bool


class DeathBucket(BaseModel):
    label: str
    from_min: int
    to_min: int | None
    deaths: int = Field(description="Hero deaths in the bucket (for swing_after).")
    clean_deaths: int = Field(description="Single, not bought-back deaths (respawn and gold).")
    respawn_s_median: float | None
    gold_lost_median: float | None
    swing_after: float | None = Field(
        description="Mean change of the victim team's gold lead over the next N minutes."
    )
    swing_ci_low: float | None
    swing_ci_high: float | None


class BuybackCell(BaseModel):
    phase: str
    lead: str = Field(description='"behind", "even" or "ahead" at the death minute.')
    buyback: Rate = Field(description="Win rate after deaths that were bought back.")
    no_buyback: Rate = Field(description="Win rate after deaths that were not.")
    difference: float | None
    p_value: float | None
    swing_after_buyback: float | None
    swing_after_no_buyback: float | None
    verdict: str = Field(description='"pays off", "does not pay off" or "unclear".')


class DeathAnalysis(BaseModel):
    patch: str
    hero: str | None
    hero_id: int | None
    later_minutes: int
    deaths: int
    player_matches: int
    gold_lost_per_death: float | None = Field(
        description="Total death gold / total deaths over all player-matches in the sample."
    )
    dead_seconds_per_death: float | None = Field(
        description="Total seconds dead / total deaths (buybacks shorten it)."
    )
    buyback_share: float | None = Field(description="Share of deaths that were bought back.")
    by_minute: list[DeathBucket]
    buybacks: list[BuybackCell]


def extract_deaths(sample: Sample, hero_id: int | None = None) -> tuple[list[Death], list[Player]]:
    """Hero deaths (from enemies' kills_log) and the victims' player-matches considered."""
    npc = _npc_names()
    matches = {m.match_id: m for m in sample.matches}
    deaths: list[Death] = []
    victims: list[Player] = []
    for match_id, roster in sample.by_match().items():
        m = matches[match_id]
        by_hero = {p.hero_id: p for p in roster}
        times: dict[int, list[int]] = defaultdict(list)
        for killer in roster:
            for k in killer.kills:
                victim = by_hero.get(npc.get(k.victim, -1))
                if victim is not None and victim.radiant != killer.radiant:
                    times[victim.slot].append(k.time)
        for p in roster:
            if hero_id is not None and p.hero_id != hero_id:
                continue
            victims.append(p)
            own = sorted(set(times[p.slot]))
            for i, t in enumerate(own):
                until = min(t + BUYBACK_WINDOW_S, own[i + 1] if i + 1 < len(own) else t + 10**6)
                bought = any(t <= b <= until for b in p.buybacks)
                deaths.append(Death(match=m, victim=p, time=t, bought_back=bought))
    return deaths, victims


def _bucket_of(minute: int) -> int:
    index = 0
    for i, edge in enumerate(DEATH_BUCKETS):
        if minute >= edge:
            index = i
    return index


def _swing(m: Match, radiant: bool, minute: int, later: int) -> int | None:
    now = m.lead(radiant, minute)
    after = m.lead(radiant, minute + later)
    return None if now is None or after is None else after - now


def death_buckets(
    deaths: Sequence[Death], victims: Sequence[Player], later: int
) -> list[DeathBucket]:
    by_victim: dict[tuple[int, int], list[Death]] = defaultdict(list)
    for d in deaths:
        by_victim[(d.match.match_id, d.victim.slot)].append(d)
    clean: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for p in victims:
        own = by_victim.get((p.match_id, p.slot), [])
        if p.deaths == 1 and len(own) == 1 and not p.buybacks and p.dead_s and p.death_gold:
            clean[_bucket_of(own[0].time // 60)].append((p.dead_s, p.death_gold))
    swings: dict[int, list[int]] = defaultdict(list)
    counts: dict[int, int] = defaultdict(int)
    for d in deaths:
        b = _bucket_of(d.time // 60)
        counts[b] += 1
        swing = _swing(d.match, d.victim.radiant, d.time // 60, later)
        if swing is not None:
            swings[b].append(swing)
    out = []
    for i, lo in enumerate(DEATH_BUCKETS):
        hi = DEATH_BUCKETS[i + 1] if i + 1 < len(DEATH_BUCKETS) else None
        rows = clean.get(i, [])
        after = swings.get(i, [])
        ci = _ci_mean(after) if after else None
        out.append(
            DeathBucket(
                label=f"{lo}-{hi}" if hi is not None else f"{lo}+",
                from_min=lo,
                to_min=hi,
                deaths=counts.get(i, 0),
                clean_deaths=len(rows),
                respawn_s_median=median(r[0] for r in rows) if rows else None,
                gold_lost_median=median(r[1] for r in rows) if rows else None,
                swing_after=mean(after) if after else None,
                swing_ci_low=ci[0] if ci else None,
                swing_ci_high=ci[1] if ci else None,
            )
        )
    return out


def _lead_state(lead: int) -> str:
    if lead <= -BUYBACK_LEAD:
        return "behind"
    if lead >= BUYBACK_LEAD:
        return "ahead"
    return "even"


def buyback_cells(deaths: Sequence[Death], later: int) -> list[BuybackCell]:
    groups: dict[tuple[str, str], list[Death]] = defaultdict(list)
    for d in deaths:
        minute = d.time // 60
        lead = d.match.lead(d.victim.radiant, minute)
        if minute < BUYBACK_FROM_MIN or lead is None:
            continue
        phase = (
            f"{BUYBACK_FROM_MIN}-{BUYBACK_LATE_MIN}"
            if minute < BUYBACK_LATE_MIN
            else f"{BUYBACK_LATE_MIN}+"
        )
        groups[(phase, _lead_state(lead))].append(d)
    out = []
    for phase in (f"{BUYBACK_FROM_MIN}-{BUYBACK_LATE_MIN}", f"{BUYBACK_LATE_MIN}+"):
        for lead in ("behind", "even", "ahead"):
            cell = groups.get((phase, lead), [])
            cmp = compare("buyback", "", ((d.bought_back, d.match.won(d.victim.radiant))
                                          for d in cell))  # fmt: skip

            def swing_of(bought: bool, cell=cell) -> float | None:
                values = [
                    s
                    for d in cell
                    if d.bought_back == bought
                    and (s := _swing(d.match, d.victim.radiant, d.time // 60, later)) is not None
                ]
                return mean(values) if values else None

            verdict = "unclear"
            if cmp.p_value is not None and cmp.p_value < ALPHA and cmp.difference is not None:
                verdict = "pays off" if cmp.difference > 0 else "does not pay off"
            out.append(
                BuybackCell(
                    phase=phase,
                    lead=lead,
                    buyback=cmp.present,
                    no_buyback=cmp.absent,
                    difference=cmp.difference,
                    p_value=cmp.p_value,
                    swing_after_buyback=swing_of(True),
                    swing_after_no_buyback=swing_of(False),
                    verdict=verdict,
                )
            )
    return out


def death_analysis(patch: str, sample: Sample, hero_id: int | None, later: int) -> DeathAnalysis:
    deaths, victims = extract_deaths(sample, hero_id)
    gold = [p for p in victims if p.death_gold is not None]
    dead = [p for p in victims if p.dead_s is not None]
    gold_deaths = sum(p.deaths for p in gold)
    dead_deaths = sum(p.deaths for p in dead)
    return DeathAnalysis(
        patch=patch,
        hero=_hero_name(hero_id) if hero_id is not None else None,
        hero_id=hero_id,
        later_minutes=later,
        deaths=len(deaths),
        player_matches=len(victims),
        gold_lost_per_death=(
            sum(p.death_gold or 0 for p in gold) / gold_deaths if gold_deaths else None
        ),
        dead_seconds_per_death=(
            sum(p.dead_s or 0 for p in dead) / dead_deaths if dead_deaths else None
        ),
        buyback_share=(sum(d.bought_back for d in deaths) / len(deaths)) if deaths else None,
        by_minute=death_buckets(deaths, victims, later),
        buybacks=buyback_cells(deaths, later),
    )
