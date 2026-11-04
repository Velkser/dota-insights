"""Hero matchups, synergies, combos, pick suggestions and draft analysis.

Data comes from STRATZ ``heroStats.matchUp`` (pair win counts against and alongside every other
hero, by rank bracket) and ``heroStats.stats(groupByPosition)`` (games per hero and position).
OpenDota ``/heroes/{id}/matchups`` is the fallback for opponents when STRATZ cannot be reached;
it has no ally data, no rank split and small samples.

Two advantage measures, both in win-rate points (0.03 = 3 points):

- matchup advantage of A against B: ``stats.advantage(wr, base_a, base_b)``, the pair win rate
  minus the log5 expectation ``a(1-b) / (a(1-b) + b(1-a))``.
- synergy advantage of A with B: ``stats.advantage(wr, base_a, 1 - base_b)``, the pair win rate
  minus ``ab / (ab + (1-a)(1-b))``, which combines the two heroes' odds of winning as if they
  were independent.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from dota_insights.analytics.stats import Interval, advantage, wilson_interval
from dota_insights.cache import TTL_AGGREGATE, cached
from dota_insights.models import Filters, Position, RankBracket
from dota_insights.reference import hero_by_id
from dota_insights.sources import get_opendota, get_stratz
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzAuthError, StratzError, StratzUnavailableError

# A hero counts as playable at a position when at least this share of its games are there.
MIN_POSITION_SHARE = 0.10
# Pick estimate is clipped to this range: summed advantages are not a calibrated model.
WIN_PROBABILITY_CLIP = (0.05, 0.95)
MAX_OPENDOTA_FALLBACK_HEROES = 10

STRATZ_BRACKETS: dict[RankBracket, str] = {
    RankBracket.HERALD: "HERALD_GUARDIAN",
    RankBracket.GUARDIAN: "HERALD_GUARDIAN",
    RankBracket.CRUSADER: "CRUSADER_ARCHON",
    RankBracket.ARCHON: "CRUSADER_ARCHON",
    RankBracket.LEGEND: "LEGEND_ANCIENT",
    RankBracket.ANCIENT: "LEGEND_ANCIENT",
    RankBracket.DIVINE: "DIVINE_IMMORTAL",
    RankBracket.IMMORTAL: "DIVINE_IMMORTAL",
}
_PAIR_MEDALS = {
    "HERALD_GUARDIAN": (RankBracket.HERALD, RankBracket.GUARDIAN),
    "CRUSADER_ARCHON": (RankBracket.CRUSADER, RankBracket.ARCHON),
    "LEGEND_ANCIENT": (RankBracket.LEGEND, RankBracket.ANCIENT),
    "DIVINE_IMMORTAL": (RankBracket.DIVINE, RankBracket.IMMORTAL),
}

MATCHUP_QUERY = """
query Matchups($heroIds: [Short], $brackets: [RankBracketBasicEnum]) {
  heroStats {
    matchUp(heroIds: $heroIds, bracketBasicIds: $brackets, take: 200) {
      heroId matchCountWith matchCountVs
      vs { heroId2 matchCount winCount synergy winRateHeroId1 winRateHeroId2 }
      with { heroId2 matchCount winCount synergy winRateHeroId1 winRateHeroId2 }
    }
  }
}
"""

POSITION_QUERY = """
query Positions($brackets: [RankBracketBasicEnum]) {
  heroStats {
    stats(bracketBasicIds: $brackets, groupByPosition: true) {
      heroId position matchCount winCount
    }
  }
}
"""


class MatchupsUnavailable(Exception):
    """No source could provide the matchup data for this query."""


def stratz_brackets(filters: Filters) -> tuple[str, ...] | None:
    """STRATZ bracket pairs covering ``filters.rank_bracket``; None means all brackets."""
    rank = filters.rank_range
    if rank is None:
        return None
    lo, hi = rank
    picked = [STRATZ_BRACKETS[b] for b in RankBracket if lo.tier <= b.tier <= hi.tier]
    return tuple(dict.fromkeys(picked))


def bracket_caveat(filters: Filters, brackets: tuple[str, ...] | None) -> str | None:
    """Say so when STRATZ's medal pairs are wider than the requested rank range."""
    rank = filters.rank_range
    if rank is None or brackets is None:
        return None
    lo, hi = rank
    covered = [m for b in brackets for m in _PAIR_MEDALS[b]]
    if covered[0] == lo and covered[-1] == hi:
        return None
    names = ", ".join(b.lower() for b in brackets)
    return (
        f"STRATZ groups medals in pairs, so rank {filters.rank_bracket!r} was widened to "
        f"{covered[0].value}-{covered[-1].value} ({names})."
    )


@dataclass(frozen=True, slots=True)
class Pair:
    """Games of ``hero`` against or alongside ``other``, from ``hero``'s side."""

    hero: int
    other: int
    games: int
    wins: int
    base_hero: float
    base_other: float
    stratz_synergy: float | None = None

    @property
    def win_rate(self) -> float:
        return self.wins / self.games

    @property
    def interval(self) -> Interval:
        return wilson_interval(self.wins, self.games)

    def flipped(self) -> Pair:
        """The same games against each other, seen from ``other``'s side."""
        return Pair(
            self.other,
            self.hero,
            self.games,
            self.games - self.wins,
            self.base_other,
            self.base_hero,
        )


def matchup_advantage(pair: Pair) -> float:
    return advantage(pair.win_rate, pair.base_hero, pair.base_other)


def synergy_advantage(pair: Pair) -> float:
    return advantage(pair.win_rate, pair.base_hero, 1 - pair.base_other)


@dataclass
class MatchupData:
    """Pair statistics for a set of heroes from one source."""

    source: Literal["stratz", "opendota"]
    brackets: tuple[str, ...] | None
    vs: dict[tuple[int, int], Pair] = field(default_factory=dict)
    with_: dict[tuple[int, int], Pair] = field(default_factory=dict)
    games: dict[int, int] = field(default_factory=dict)
    base: dict[int, float] = field(default_factory=dict)
    has_synergy: bool = True

    def against(self, hero: int, other: int) -> Pair | None:
        """``hero``'s games against ``other``, using ``other``'s row when ``hero`` has none."""
        if (hero, other) in self.vs:
            return self.vs[(hero, other)]
        if (other, hero) in self.vs:
            return self.vs[(other, hero)].flipped()
        return None

    def alongside(self, hero: int, other: int) -> Pair | None:
        if (hero, other) in self.with_:
            return self.with_[(hero, other)]
        if (other, hero) in self.with_:
            p = self.with_[(other, hero)]
            return Pair(hero, other, p.games, p.wins, p.base_other, p.base_hero)
        return None


@dataclass(frozen=True, slots=True)
class PositionStats:
    """Games per hero and position (STRATZ position detection)."""

    games: dict[int, dict[int, int]]
    wins: dict[int, dict[int, int]]

    def share(self, hero: int, position: int) -> float:
        by_pos = self.games.get(hero, {})
        total = sum(by_pos.values())
        return by_pos.get(position, 0) / total if total else 0.0

    def heroes_at(self, position: int, min_share: float = MIN_POSITION_SHARE) -> set[int]:
        return {h for h in self.games if self.share(h, position) >= min_share}

    def main_positions(self, hero: int, min_share: float = MIN_POSITION_SHARE) -> list[int]:
        by_pos = self.games.get(hero, {})
        return sorted(
            (p for p in by_pos if self.share(hero, p) >= min_share), key=lambda p: -by_pos[p]
        )


# Source calls


@cached(ttl=TTL_AGGREGATE)
async def _stratz_matchups(hero_ids: tuple[int, ...], brackets: tuple[str, ...] | None) -> Any:
    variables: dict[str, Any] = {"heroIds": list(hero_ids)}
    if brackets:
        variables["brackets"] = list(brackets)
    return await get_stratz().query(MATCHUP_QUERY, variables)


@cached(ttl=TTL_AGGREGATE)
async def _stratz_positions(brackets: tuple[str, ...] | None) -> Any:
    return await get_stratz().query(
        POSITION_QUERY, {"brackets": list(brackets)} if brackets else {}
    )


@cached(ttl=TTL_AGGREGATE)
async def _opendota_matchups(hero_id: int) -> Any:
    return await get_opendota().hero_matchups(hero_id)


@cached(ttl=TTL_AGGREGATE)
async def _opendota_hero_stats() -> Any:
    return await get_opendota().hero_stats()


def parse_stratz_matchups(data: Any, brackets: tuple[str, ...] | None) -> MatchupData:
    out = MatchupData(source="stratz", brackets=brackets)
    for row in (data or {}).get("heroStats", {}).get("matchUp") or []:
        hero = row["heroId"]
        # Every game puts five opponents in ``vs``, so the pair counts sum to 5x the games.
        out.games[hero] = int(row.get("matchCountVs") or 0) // 5
        for kind, target in (("vs", out.vs), ("with", out.with_)):
            for p in row.get(kind) or []:
                if not p.get("matchCount"):
                    continue
                out.base.setdefault(hero, float(p["winRateHeroId1"]))
                out.base.setdefault(p["heroId2"], float(p["winRateHeroId2"]))
                target[(hero, p["heroId2"])] = Pair(
                    hero=hero,
                    other=p["heroId2"],
                    games=int(p["matchCount"]),
                    wins=int(p["winCount"]),
                    base_hero=float(p["winRateHeroId1"]),
                    base_other=float(p["winRateHeroId2"]),
                    stratz_synergy=p.get("synergy"),
                )
    return out


def parse_opendota_matchups(rows_by_hero: dict[int, Any], hero_stats: Any) -> MatchupData:
    out = MatchupData(source="opendota", brackets=None, has_synergy=False)
    for h in hero_stats or []:
        if h.get("pub_pick"):
            out.base[h["id"]] = h["pub_win"] / h["pub_pick"]
    for hero, rows in rows_by_hero.items():
        total = 0
        for r in rows or []:
            other, games = r["hero_id"], int(r["games_played"])
            total += games
            if games and hero in out.base and other in out.base:
                out.vs[(hero, other)] = Pair(
                    hero, other, games, int(r["wins"]), out.base[hero], out.base[other]
                )
        out.games[hero] = total // 5
    return out


def parse_positions(data: Any) -> PositionStats:
    games: dict[int, dict[int, int]] = {}
    wins: dict[int, dict[int, int]] = {}
    for row in (data or {}).get("heroStats", {}).get("stats") or []:
        pos = str(row.get("position") or "")
        if not pos.startswith("POSITION_"):
            continue
        p = int(pos.removeprefix("POSITION_"))
        games.setdefault(row["heroId"], {})[p] = int(row["matchCount"])
        wins.setdefault(row["heroId"], {})[p] = int(row["winCount"])
    return PositionStats(games, wins)


async def load_matchups(hero_ids: Iterable[int] | None, filters: Filters) -> MatchupData:
    """Pairs for ``hero_ids`` (None = every hero) from STRATZ, else OpenDota opponents only."""
    brackets = stratz_brackets(filters)
    ids = tuple(sorted(set(hero_ids))) if hero_ids is not None else tuple(sorted(hero_by_id()))
    try:
        return parse_stratz_matchups(await _stratz_matchups(ids, brackets), brackets)
    except (StratzAuthError, StratzUnavailableError) as exc:
        stratz_problem = str(exc)
    except StratzError as exc:
        raise MatchupsUnavailable(f"STRATZ matchup query failed: {exc}") from exc
    if brackets is not None:
        raise MatchupsUnavailable(
            f"{stratz_problem}. The OpenDota fallback has no rank split, so it cannot answer "
            "a rank-filtered query."
        )
    if len(ids) > MAX_OPENDOTA_FALLBACK_HEROES:
        raise MatchupsUnavailable(
            f"{stratz_problem}. The OpenDota fallback needs one call per hero and is limited "
            f"to {MAX_OPENDOTA_FALLBACK_HEROES} heroes."
        )
    try:
        rows = {h: await _opendota_matchups(h) for h in ids}
        return parse_opendota_matchups(rows, await _opendota_hero_stats())
    except OpenDotaError as exc:
        raise MatchupsUnavailable(f"{stratz_problem}; OpenDota also failed: {exc}") from exc


async def load_positions(filters: Filters) -> PositionStats | None:
    """Games per hero and position, or None when STRATZ is unavailable."""
    try:
        return parse_positions(await _stratz_positions(stratz_brackets(filters)))
    except StratzError:
        return None


# Calculations


def hero_name(hero_id: int) -> str:
    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"hero {hero_id}"


def _pct(x: float) -> str:
    return f"{x * 100:+.1f} pts"


def pair_entry(pair: Pair, adv: float) -> dict[str, Any]:
    low, high = pair.interval
    entry: dict[str, Any] = {
        "hero": hero_name(pair.other),
        "hero_id": pair.other,
        "games": pair.games,
        "win_rate": round(pair.win_rate, 4),
        "win_rate_ci": [round(low, 4), round(high, 4)],
        "advantage": round(adv, 4),
        "base_win_rate": round(pair.base_other, 4),
    }
    if pair.stratz_synergy is not None:
        entry["stratz_synergy"] = pair.stratz_synergy
    return entry


def hero_matchups(
    data: MatchupData,
    hero: int,
    *,
    mode: Literal["counters", "synergies", "both"],
    min_pair_games: int,
    limit: int,
    others: set[int] | None = None,
) -> dict[str, Any]:
    """Counters / easy matchups and best / worst allies of ``hero`` from its own side."""
    result: dict[str, Any] = {}
    skipped = 0

    def ranked(pairs: dict[tuple[int, int], Pair], score) -> list[tuple[Pair, float]]:
        nonlocal skipped
        rows = []
        for (h, other), p in pairs.items():
            if h != hero or (others is not None and other not in others):
                continue
            if p.games < min_pair_games:
                skipped += 1
                continue
            rows.append((p, score(p)))
        return sorted(rows, key=lambda r: r[1])

    if mode in ("counters", "both"):
        rows = ranked(data.vs, matchup_advantage)
        result["counters"] = [pair_entry(p, a) for p, a in rows[:limit] if a < 0]
        result["easiest_matchups"] = [pair_entry(p, a) for p, a in rows[::-1][:limit] if a > 0]
    if mode in ("synergies", "both") and data.has_synergy:
        rows = ranked(data.with_, synergy_advantage)
        result["best_allies"] = [pair_entry(p, a) for p, a in rows[::-1][:limit] if a > 0]
        result["worst_allies"] = [pair_entry(p, a) for p, a in rows[:limit] if a < 0]
    result["pairs_below_min_sample"] = skipped
    return result


def find_combos(
    data: MatchupData,
    *,
    size: Literal[2, 3],
    slots: Sequence[set[int]] | None,
    min_pair_games: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Best ally duos or trios by summed pairwise synergy advantage.

    ``slots`` gives the eligible heroes for each slot (by position); None means any hero.
    A trio scores the sum of its three pair advantages and needs every pair to meet
    ``min_pair_games``; STRATZ has no three-hero aggregates, so this is an estimate from pairs.
    """
    usable: dict[frozenset[int], tuple[Pair, float]] = {}
    for (a, b), p in data.with_.items():
        key = frozenset((a, b))
        if a == b or p.games < min_pair_games or key in usable:
            continue
        usable[key] = (p, synergy_advantage(p))
    heroes = sorted({h for k in usable for h in k})

    if slots is None:
        groups: Iterable[tuple[int, ...]] = itertools.combinations(heroes, size)
    else:
        seen: set[frozenset[int]] = set()

        def by_slots() -> Iterable[tuple[int, ...]]:
            for combo in itertools.product(*(sorted(s & set(heroes)) for s in slots)):
                key = frozenset(combo)
                if len(key) == size and key not in seen:
                    seen.add(key)
                    yield combo

        groups = by_slots()

    scored = []
    for combo in groups:
        pairs = [usable.get(frozenset(pq)) for pq in itertools.combinations(combo, 2)]
        if any(p is None for p in pairs):
            continue
        score = sum(a for _, a in pairs)  # type: ignore[misc]
        scored.append((score, combo, pairs))
    scored.sort(key=lambda s: -s[0])

    out = []
    for score, combo, pairs in scored[:limit]:
        entry: dict[str, Any] = {
            "heroes": [hero_name(h) for h in combo],
            "hero_ids": list(combo),
            "synergy_advantage": round(score, 4),
            "min_pair_games": min(p.games for p, _ in pairs),
        }
        if size == 2:
            p, _ = pairs[0]
            low, high = p.interval
            entry.update(
                games=p.games,
                win_rate=round(p.win_rate, 4),
                win_rate_ci=[round(low, 4), round(high, 4)],
            )
        else:
            entry["pairs"] = [
                {
                    "heroes": [hero_name(p.hero), hero_name(p.other)],
                    "games": p.games,
                    "win_rate": round(p.win_rate, 4),
                    "synergy_advantage": round(a, 4),
                }
                for p, a in pairs
            ]
        out.append(entry)
    return out


@dataclass(frozen=True, slots=True)
class Term:
    kind: Literal["vs", "with"]
    other: int
    value: float
    games: int

    def reason(self, hero: int) -> str:
        verb = "against" if self.kind == "vs" else "with"
        return (
            f"{hero_name(hero)} {verb} {hero_name(self.other)}: {_pct(self.value)} "
            f"over {self.games} games"
        )


def _terms(
    data: MatchupData, hero: int, allies: Sequence[int], enemies: Sequence[int], min_pair_games: int
) -> tuple[list[Term], list[str]]:
    terms: list[Term] = []
    missing: list[str] = []
    for e in enemies:
        p = data.against(hero, e)
        if p is None or p.games < min_pair_games:
            missing.append(f"vs {hero_name(e)}")
        else:
            terms.append(Term("vs", e, matchup_advantage(p), p.games))
    for a in allies:
        if not data.has_synergy:
            missing.append(f"with {hero_name(a)}")
            continue
        p = data.alongside(hero, a)
        if p is None or p.games < min_pair_games:
            missing.append(f"with {hero_name(a)}")
        else:
            terms.append(Term("with", a, synergy_advantage(p), p.games))
    return terms, missing


def suggest_picks(
    data: MatchupData,
    *,
    allies: Sequence[int],
    enemies: Sequence[int],
    candidates: Iterable[int],
    min_pair_games: int,
    limit: int,
    reasons: int = 3,
) -> list[dict[str, Any]]:
    """Rank ``candidates`` by summed advantage against ``enemies`` and synergy with ``allies``."""
    rows = []
    for c in candidates:
        terms, missing = _terms(data, c, allies, enemies, min_pair_games)
        if not terms:
            continue
        vs_sum = sum(t.value for t in terms if t.kind == "vs")
        with_sum = sum(t.value for t in terms if t.kind == "with")
        top = sorted(terms, key=lambda t: -abs(t.value))[:reasons]
        rows.append(
            {
                "hero": hero_name(c),
                "hero_id": c,
                "score": round(vs_sum + with_sum, 4),
                "matchup_advantage_sum": round(vs_sum, 4),
                "synergy_advantage_sum": round(with_sum, 4),
                "base_win_rate": round(data.base[c], 4) if c in data.base else None,
                "pairs_used": len(terms),
                "pairs_missing": missing,
                "reasons": [t.reason(c) for t in top],
            }
        )
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def analyze_draft(
    data: MatchupData,
    *,
    radiant: Sequence[int],
    dire: Sequence[int],
    min_pair_games: int,
    positions: PositionStats | None,
) -> dict[str, Any]:
    """Per-hero and pairwise contributions plus a heuristic Radiant win estimate."""
    cross = []
    for r, d in itertools.product(radiant, dire):
        p = data.against(r, d)
        if p is not None and p.games >= min_pair_games:
            cross.append((r, d, matchup_advantage(p), p.games))

    def team_synergy(team: Sequence[int]) -> list[tuple[int, int, float, int]]:
        out = []
        if not data.has_synergy:
            return out
        for a, b in itertools.combinations(team, 2):
            p = data.alongside(a, b)
            if p is not None and p.games >= min_pair_games:
                out.append((a, b, synergy_advantage(p), p.games))
        return out

    syn = {"radiant": team_synergy(radiant), "dire": team_synergy(dire)}

    def per_hero(team: Sequence[int], side: str) -> list[dict[str, Any]]:
        rows = []
        for h in team:
            # Cross advantages are stored from Radiant's side; flip them for Dire heroes.
            sign = 1 if side == "radiant" else -1
            vs_sum = sum(sign * a for r, d, a, _ in cross if h in (r, d))
            with_sum = sum(s for a, b, s, _ in syn[side] if h in (a, b))
            row: dict[str, Any] = {
                "hero": hero_name(h),
                "hero_id": h,
                "base_win_rate": round(data.base[h], 4) if h in data.base else None,
                "matchup_advantage_sum": round(vs_sum, 4),
                "synergy_advantage_sum": round(with_sum, 4),
            }
            if positions is not None:
                row["usual_positions"] = positions.main_positions(h)
            rows.append(row)
        return rows

    def mean_base(team: Sequence[int]) -> float | None:
        vals = [data.base[h] for h in team if h in data.base]
        return sum(vals) / len(vals) if vals else None

    base_r, base_d = mean_base(radiant), mean_base(dire)
    d_base = (base_r - base_d) if base_r is not None and base_d is not None else 0.0
    d_matchup = sum(a for *_, a, _ in cross) / 5
    d_synergy = (sum(s for *_, s, _ in syn["radiant"]) - sum(s for *_, s, _ in syn["dire"])) / 5
    raw = 0.5 + d_base + d_matchup + d_synergy
    lo, hi = WIN_PROBABILITY_CLIP
    estimate = min(hi, max(lo, raw))

    pairwise = {
        "matchups": sorted(
            (
                {
                    "radiant": hero_name(r),
                    "dire": hero_name(d),
                    "radiant_advantage": round(a, 4),
                    "games": g,
                }
                for r, d, a, g in cross
            ),
            key=lambda x: -abs(x["radiant_advantage"]),
        ),
        "radiant_synergy": [
            {"heroes": [hero_name(a), hero_name(b)], "synergy_advantage": round(s, 4), "games": g}
            for a, b, s, g in syn["radiant"]
        ],
        "dire_synergy": [
            {"heroes": [hero_name(a), hero_name(b)], "synergy_advantage": round(s, 4), "games": g}
            for a, b, s, g in syn["dire"]
        ],
    }
    return {
        "radiant_win_estimate": round(estimate, 4),
        "estimate_components": {
            "base_strength": round(d_base, 4),
            "matchups": round(d_matchup, 4),
            "synergy": round(d_synergy, 4),
        },
        "radiant": per_hero(radiant, "radiant"),
        "dire": per_hero(dire, "dire"),
        "pairwise": pairwise,
        "pairs_used": len(cross) + len(syn["radiant"]) + len(syn["dire"]),
        "pairs_possible": 25 + (20 if data.has_synergy else 0),
        "weaknesses": {
            "radiant": draft_weaknesses(radiant, cross, syn["radiant"], positions, "radiant"),
            "dire": draft_weaknesses(dire, cross, syn["dire"], positions, "dire"),
        },
    }


def draft_weaknesses(
    team: Sequence[int],
    cross: list[tuple[int, int, float, int]],
    synergy: list[tuple[int, int, float, int]],
    positions: PositionStats | None,
    side: str,
    threshold: float = 0.02,
) -> list[str]:
    """Weak spots visible in the data: hard counters, poor pairs, uncovered positions, roles."""
    out = []
    for r, d, a, g in cross:
        mine, theirs, adv = (r, d, a) if side == "radiant" else (d, r, -a)
        if adv <= -threshold:
            out.append(
                f"{hero_name(mine)} is countered by {hero_name(theirs)} ({_pct(adv)}, {g} games)"
            )
    for a, b, s, g in synergy:
        if s <= -threshold:
            out.append(
                f"{hero_name(a)} and {hero_name(b)} win less together than expected "
                f"({_pct(s)}, {g} games)"
            )
    if positions is not None:
        for pos in Position:
            if not any(positions.share(h, pos) >= MIN_POSITION_SHARE for h in team):
                out.append(
                    f"No hero in this draft is commonly played at position {pos.value} "
                    f"({pos.name.lower().replace('_', ' ')})"
                )
    heroes = hero_by_id()
    roles = [set(heroes[h].roles) for h in team if h in heroes]
    for role in ("Disabler", "Initiator"):
        if roles and not any(role in r for r in roles):
            out.append(f"No hero tagged {role} (Valve role tags)")
    return out
