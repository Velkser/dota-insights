"""Hero builds: items, skill order, talents, Aghanim's upgrades and neutral items.

Data comes from STRATZ ``heroStats`` aggregates (item purchases, starting items, boots,
talents, ability max levels; filterable by paired rank bracket and position) and from STRATZ
"guide" matches (recent high-rank matches STRATZ picked as guides; every one we sampled was
a win). Neutral items come from STRATZ ``itemNeutral`` when it has rows and otherwise from
OpenDota's explorer over league matches. OpenDota ``/heroes/{id}/itemPopularity`` is the
fallback for item builds when STRATZ is unavailable.

Live checks on 2026-11-04 (patch 7.41f) that shaped this module:

- Item, boot, starting-item and talent rows carry no total match count, and
  ``heroStats.stats`` covers a different window (27k vs ~41k matches for Anti-Mage pos 1,
  Divine-Immortal). The denominator for popularity is therefore the number of matches in
  which a level-10 talent was picked (see ``reference_matches``).
- ``itemFullPurchase.time`` is the purchase minute; rows are per (item, instance, minute).
- ``itemNeutral`` returned no rows for any hero or bracket.
- STRATZ rejects queries above a complexity of 310,000, so guide matches are fetched in
  batches of ``GUIDE_BATCH`` aliased ``match(id)`` lookups.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from dota_insights.analytics.stats import wilson_interval
from dota_insights.cache import TTL_AGGREGATE, TTL_REFERENCE, cached
from dota_insights.models.filters import Filters, Position
from dota_insights.reference import ability_by_name, item_by_id, item_ids, patches
from dota_insights.sources import get_opendota, get_stratz

# STRATZ pairs medals; Filters uses single medals (tier 1 = Herald .. 8 = Immortal).
BRACKET_PAIRS: tuple[tuple[str, tuple[int, int]], ...] = (
    ("HERALD_GUARDIAN", (1, 2)),
    ("CRUSADER_ARCHON", (3, 4)),
    ("LEGEND_ANCIENT", (5, 6)),
    ("DIVINE_IMMORTAL", (7, 8)),
)

SCEPTER_ID = 108
SHARD_ID = 609
BLESSING_ID = 271  # Aghanim's Blessing (scepter consumed)

# Item phases from itemFullPurchase, by median purchase minute and popularity.
EARLY_BEFORE_MINUTE = 14
CORE_MIN_POPULARITY = 0.40
SITUATIONAL_MIN_POPULARITY = 0.05
EARLY_MIN_POPULARITY = 0.10

TIMING_BUCKETS: tuple[tuple[int, int | None], ...] = (
    (0, 15),
    (15, 20),
    (20, 25),
    (25, 30),
    (30, 40),
    (40, None),
)

GUIDE_LIMIT = 60
GUIDE_BATCH = 20
SKILL_ORDER_LEVELS = 10

AGGREGATES_QUERY = """
query Builds($h: Short!, $b: [RankBracketBasicEnum], $p: [MatchPlayerPositionType]) {
  heroStats {
    itemFullPurchase(heroId: $h, bracketBasicIds: $b, positionIds: $p) {
      itemId instance time matchCount winCount
    }
    itemStartingPurchase(heroId: $h, bracketBasicIds: $b, positionIds: $p) {
      itemId instance wasGiven matchCount winCount
    }
    itemBootPurchase(heroId: $h, bracketBasicIds: $b, positionIds: $p) {
      itemId instance timeAverage matchCount winCount
    }
    talent(heroId: $h, bracketBasicIds: $b, positionIds: $p) {
      abilityId matchCount winCount timeAverage
    }
    abilityMaxLevel(heroId: $h, bracketBasicIds: $b, positionIds: $p) {
      abilityId level matchCount winCount
    }
  }
  constants {
    hero(id: $h) {
      id
      abilities { slot abilityId ability { name } }
      talents { slot abilityId }
      facets { slot facetId }
    }
  }
}
"""

NEUTRAL_QUERY = """
query Neutral($h: Short!, $b: [RankBracketBasicEnum], $p: [MatchPlayerPositionType]) {
  heroStats {
    itemNeutral(heroId: $h, bracketBasicIds: $b, positionIds: $p) {
      itemId matchCount winCount
    }
  }
}
"""

GUIDES_QUERY = """
query Guides($h: Short!, $p: MatchPlayerPositionType, $take: Int!) {
  heroStats {
    guide(heroId: $h, positionId: $p, take: 1) {
      matchCount
      guides(take: $take) { matchId steamAccountId }
    }
  }
}
"""

GUIDE_MATCH_FIELDS = (
    "id gameVersionId startDateTime bracket "
    "players(steamAccountId: {account}) {{ heroId isVictory variant position neutral0Id "
    "abilities {{ abilityId level }} }}"
)


# --- Filters -> STRATZ arguments ---------------------------------------------------------


def stratz_brackets(filters: Filters) -> tuple[tuple[str, ...], tuple[int, int] | None]:
    """STRATZ bracket pairs covering ``filters.rank_bracket`` and the medal tiers they span.

    Returns ``((), None)`` for all brackets. The tiers show when a pair widens the request
    (``"immortal"`` becomes Divine + Immortal).
    """
    rank = filters.rank_range
    if rank is None:
        return (), None
    lo, hi = rank[0].tier, rank[1].tier
    chosen = [(name, tiers) for name, tiers in BRACKET_PAIRS if tiers[1] >= lo and tiers[0] <= hi]
    return tuple(name for name, _ in chosen), (chosen[0][1][0], chosen[-1][1][1])


def stratz_position(position: Position | None) -> str | None:
    return None if position is None else f"POSITION_{int(position)}"


# --- Source calls ------------------------------------------------------------------------


@cached(ttl=TTL_AGGREGATE)
async def fetch_aggregates(
    hero_id: int, brackets: tuple[str, ...], position: str | None
) -> dict[str, Any]:
    """STRATZ item, talent and ability aggregates plus the hero's ability/talent ids."""
    variables = {"h": hero_id, "b": list(brackets) or None, "p": [position] if position else None}
    data = await get_stratz().query(AGGREGATES_QUERY, variables)
    return {**data, "fetched_at": datetime.now(UTC)}


@cached(ttl=TTL_REFERENCE)
async def fetch_ability_names(ability_ids: tuple[int, ...]) -> dict[int, str]:
    """STRATZ ability id -> internal name (``special_bonus_...`` for talents)."""
    if not ability_ids:
        return {}
    fields = " ".join(f"a{i}: ability(id: {i}) {{ id name }}" for i in ability_ids)
    data = await get_stratz().query(f"query {{ constants {{ {fields} }} }}")
    return {
        row["id"]: row["name"]
        for row in (data.get("constants") or {}).values()
        if row and row.get("name")
    }


@dataclass(frozen=True, slots=True)
class GuideMatch:
    match_id: int
    game_version_id: int | None
    start: datetime | None
    bracket: int | None
    won: bool | None
    variant: int | None
    neutral_item_id: int | None
    ability_ids: tuple[int, ...]  # in the order they were learned


@cached(ttl=TTL_AGGREGATE)
async def fetch_guides(
    hero_id: int, position: str | None, limit: int = GUIDE_LIMIT
) -> tuple[int, tuple[GuideMatch, ...]]:
    """STRATZ guide matches for a hero: (guide pool size, sampled matches)."""
    stratz = get_stratz()
    data = await stratz.query(GUIDES_QUERY, {"h": hero_id, "p": position, "take": limit})
    lists = (data.get("heroStats") or {}).get("guide") or []
    if not lists:
        return 0, ()
    pool = lists[0].get("matchCount") or 0
    refs = [(g["matchId"], g["steamAccountId"]) for g in lists[0].get("guides") or []]
    matches: list[GuideMatch] = []
    for start in range(0, len(refs), GUIDE_BATCH):
        batch = refs[start : start + GUIDE_BATCH]
        fields = " ".join(
            f"m{i}: match(id: {match_id}) {{ " + GUIDE_MATCH_FIELDS.format(account=account) + " }"
            for i, (match_id, account) in enumerate(batch)
        )
        rows = await stratz.query(f"query {{ {fields} }}")
        for i in range(len(batch)):
            match = (rows or {}).get(f"m{i}")
            if not match:
                continue
            player = next(
                (p for p in match.get("players") or [] if p.get("heroId") == hero_id), None
            )
            if player is None:
                continue
            started = match.get("startDateTime")
            matches.append(
                GuideMatch(
                    match_id=match["id"],
                    game_version_id=match.get("gameVersionId"),
                    start=datetime.fromtimestamp(started, UTC) if started else None,
                    bracket=match.get("bracket"),
                    won=player.get("isVictory"),
                    variant=player.get("variant"),
                    neutral_item_id=player.get("neutral0Id"),
                    ability_ids=tuple(a["abilityId"] for a in player.get("abilities") or []),
                )
            )
    return pool, tuple(matches)


@cached(ttl=TTL_AGGREGATE)
async def fetch_stratz_neutrals(
    hero_id: int, brackets: tuple[str, ...], position: str | None
) -> list[dict[str, Any]]:
    variables = {"h": hero_id, "b": list(brackets) or None, "p": [position] if position else None}
    data = await get_stratz().query(NEUTRAL_QUERY, variables)
    return (data.get("heroStats") or {}).get("itemNeutral") or []


def neutral_sql(patch: str, hero_id: int | None) -> str:
    """Explorer SQL: neutral item held at match end, per item, in league matches of a patch."""
    if not patch.replace(".", "").isalnum():
        raise ValueError(f"unexpected patch name {patch!r}")
    hero = f"pm.hero_id = {int(hero_id)} AND " if hero_id is not None else ""
    return (
        "SELECT pm.item_neutral, count(*) AS games, "
        "sum(CASE WHEN (pm.player_slot < 128) = m.radiant_win THEN 1 ELSE 0 END) AS wins, "
        "min(m.start_time) AS first_start, max(m.start_time) AS last_start "
        "FROM player_matches pm JOIN matches m USING (match_id) "
        "JOIN match_patch mp USING (match_id) "
        f"WHERE {hero}mp.patch = '{patch}' AND pm.item_neutral > 0 "
        "GROUP BY pm.item_neutral ORDER BY games DESC"
    )


@cached(ttl=TTL_AGGREGATE)
async def fetch_opendota_neutrals(patch: str, hero_id: int | None) -> list[dict[str, Any]]:
    data = await get_opendota().explorer(neutral_sql(patch, hero_id))
    if data.get("err"):
        raise RuntimeError(f"OpenDota explorer error: {data['err']}")
    return data.get("rows") or []


@cached(ttl=TTL_REFERENCE)
async def fetch_neutral_tiers() -> dict[int, int]:
    """Item id -> neutral tier (1-5) from OpenDota constants (not in our reference snapshot)."""
    data = await get_opendota().constants("items")
    return {
        int(v["id"]): int(v["tier"])
        for v in data.values()
        if isinstance(v, dict) and v.get("tier") and v.get("id") is not None
    }


@cached(ttl=TTL_AGGREGATE)
async def fetch_item_popularity(hero_id: int) -> dict[str, dict[str, int]]:
    return await get_opendota().hero_item_popularity(hero_id)


# --- Names -------------------------------------------------------------------------------


def item_name(item_id: int) -> tuple[str, str]:
    """(internal name, display name) for an item id."""
    item = item_by_id().get(item_id)
    if item is not None:
        return item.name, item.display_name or item.name
    name = item_ids().get(item_id, f"item_{item_id}")
    return name, name.replace("_", " ").title()


_TEMPLATE_VALUE = re.compile(r"\{s:[^}]+\}")


def ability_display(name: str | None, ability_id: int) -> str:
    """Display name; talent templates such as ``+{s:bonus_x} Blink Range`` show ``X``."""
    if name is None:
        return f"ability {ability_id}"
    ability = ability_by_name().get(name)
    return _TEMPLATE_VALUE.sub("X", ability.display_name) if ability else name


def current_patch() -> str:
    return patches()[-1].name


# --- Calculations ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rate:
    n: int
    wins: int

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.n if self.n else None

    @property
    def ci(self) -> tuple[float, float]:
        return tuple(wilson_interval(self.wins, self.n))  # type: ignore[return-value]


def weighted_median(buckets: Iterable[tuple[float, int]]) -> float | None:
    """Median of a value distribution given as (value, count) pairs."""
    rows = sorted((v, c) for v, c in buckets if c > 0)
    total = sum(c for _, c in rows)
    if total == 0:
        return None
    return _value_at_rank(rows, (total - 1) / 2)


def median_ci(buckets: Iterable[tuple[float, int]]) -> tuple[float, float] | None:
    """Distribution-free 95% CI for the median from grouped counts.

    Uses the order statistics at ranks ``n/2 ± 1.96·√n/2`` (normal approximation to the
    binomial), so with values binned to whole minutes the bounds are whole minutes too.
    """
    rows = sorted((v, c) for v, c in buckets if c > 0)
    n = sum(c for _, c in rows)
    if n == 0:
        return None
    half_width = 1.96 * math.sqrt(n) / 2
    lo = max(0, math.floor(n / 2 - half_width))
    hi = min(n - 1, math.ceil(n / 2 + half_width) - 1)
    return _value_at_rank(rows, lo), _value_at_rank(rows, hi)


def _value_at_rank(rows: Sequence[tuple[float, int]], rank: float) -> float:
    """Value at a 0-based (possibly fractional) rank in grouped sorted data."""
    lo_rank, hi_rank = math.floor(rank), math.ceil(rank)

    def at(r: int) -> float:
        seen = 0
        for value, count in rows:
            seen += count
            if r < seen:
                return value
        return rows[-1][0]

    return (at(lo_rank) + at(hi_rank)) / 2


def reference_matches(aggregates: dict[str, Any]) -> int:
    """Denominator for popularity: matches in which a level-10 talent was picked.

    STRATZ item rows have no total match count. Nearly every match reaches level 10, so
    the level-10 talent total is the closest available count of matches in the window. It
    is raised to the most-bought item's count if that is larger, so no rate exceeds 100%.
    """
    stats = aggregates.get("heroStats") or {}
    hero = (aggregates.get("constants") or {}).get("hero") or {}
    level10 = {t["abilityId"] for t in hero.get("talents") or [] if t["slot"] in (0, 1)}
    talent_total = sum(
        t["matchCount"] for t in stats.get("talent") or [] if t["abilityId"] in level10
    )
    biggest = max(
        (
            r["matchCount"]
            for key in ("itemFullPurchase", "itemStartingPurchase", "itemBootPurchase")
            for r in stats.get(key) or []
            if r.get("instance", 0) == 0
        ),
        default=0,
    )
    return max(talent_total, biggest)


def _item_row(item_id: int, n: int, wins: int, total: int, **extra: Any) -> dict[str, Any]:
    name, display = item_name(item_id)
    rate = Rate(n, wins)
    return {
        "item_id": item_id,
        "item": name,
        "name": display,
        "matches": n,
        "popularity": round(n / total, 4) if total else None,
        "win_rate": round(rate.win_rate, 4) if rate.win_rate is not None else None,
        "win_rate_ci": [round(x, 4) for x in rate.ci],
        **extra,
    }


def starting_items(rows: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    """Bought (not teammate-given) starting items: share of matches and typical quantity."""
    by_item: dict[int, dict[int, tuple[int, int]]] = defaultdict(dict)
    for r in rows:
        if r.get("wasGiven"):
            continue
        by_item[r["itemId"]][r["instance"]] = (r["matchCount"], r["winCount"])
    result = []
    for item_id, instances in by_item.items():
        if 0 not in instances:
            continue
        n, wins = instances[0]
        # instance k = the (k+1)-th copy; the most common quantity is the last instance
        # still bought in at least half the matches that bought one.
        quantity = 1 + sum(1 for k, (c, _) in instances.items() if k > 0 and c >= n / 2)
        result.append(_item_row(item_id, n, wins, total, typical_quantity=quantity))
    return sorted(result, key=lambda r: -r["matches"])


def boots(rows: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    result = []
    for r in rows:
        if r.get("instance", 0) != 0:
            continue
        avg = r.get("timeAverage")
        result.append(
            _item_row(
                r["itemId"],
                r["matchCount"],
                r["winCount"],
                total,
                average_minute=round(avg / 60, 1) if avg is not None else None,
            )
        )
    return sorted(result, key=lambda r: -r["matches"])


def _is_build_item(item_id: int) -> bool:
    item = item_by_id().get(item_id)
    if item is None:
        return True
    return not item.is_recipe and item.quality != "consumable"


def purchase_timings(rows: list[dict[str, Any]]) -> dict[int, list[tuple[int, int, int]]]:
    """Item id -> [(minute, matches, wins)] for the first copy bought."""
    by_item: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for r in rows:
        if r.get("instance", 0) == 0:
            by_item[r["itemId"]].append((r["time"], r["matchCount"], r["winCount"]))
    return by_item


def item_phases(rows: list[dict[str, Any]], total: int) -> dict[str, list[dict[str, Any]]]:
    """Split full purchases into early, core and situational items (thresholds above)."""
    phases: dict[str, list[dict[str, Any]]] = {"early": [], "core": [], "situational": []}
    for item_id, timings in purchase_timings(rows).items():
        if not _is_build_item(item_id):
            continue
        n = sum(m for _, m, _ in timings)
        wins = sum(w for _, _, w in timings)
        minutes = [(t, m) for t, m, _ in timings]
        median = weighted_median(minutes)
        ci = median_ci(minutes)
        row = _item_row(
            item_id,
            n,
            wins,
            total,
            median_minute=median,
            median_minute_ci=list(ci) if ci else None,
        )
        popularity = row["popularity"] or 0
        if median is not None and median < EARLY_BEFORE_MINUTE:
            if popularity >= EARLY_MIN_POPULARITY:
                phases["early"].append(row)
        elif popularity >= CORE_MIN_POPULARITY:
            phases["core"].append(row)
        elif popularity >= SITUATIONAL_MIN_POPULARITY:
            phases["situational"].append(row)
    for key, items in phases.items():
        if key == "situational":
            items.sort(key=lambda r: -r["matches"])
        else:
            items.sort(key=lambda r: r["median_minute"])
    return phases


def timing_buckets(timings: list[tuple[int, int, int]]) -> list[dict[str, Any]]:
    """Win rate by purchase-minute bucket (``TIMING_BUCKETS``)."""
    result = []
    for lo, hi in TIMING_BUCKETS:
        rows = [(m, w) for t, m, w in timings if t >= lo and (hi is None or t < hi)]
        n = sum(m for m, _ in rows)
        if n == 0:
            continue
        wins = sum(w for _, w in rows)
        rate = Rate(n, wins)
        result.append(
            {
                "minutes": f"{lo}-{hi}" if hi is not None else f"{lo}+",
                "matches": n,
                "win_rate": round(rate.win_rate, 4),
                "win_rate_ci": [round(x, 4) for x in rate.ci],
            }
        )
    return result


def aghanims(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """Scepter and Shard: purchase rate, median minute and win rate by timing bucket."""
    timings = purchase_timings(rows)
    result: dict[str, Any] = {}
    for key, item_id in (("scepter", SCEPTER_ID), ("shard", SHARD_ID)):
        rows_for = timings.get(item_id)
        if not rows_for:
            result[key] = None
            continue
        n = sum(m for _, m, _ in rows_for)
        wins = sum(w for _, _, w in rows_for)
        minutes = [(t, m) for t, m, _ in rows_for]
        ci = median_ci(minutes)
        result[key] = {
            **_item_row(item_id, n, wins, total),
            "purchase_rate": round(n / total, 4) if total else None,
            "median_minute": weighted_median(minutes),
            "median_minute_ci": list(ci) if ci else None,
            "by_timing": timing_buckets(rows_for),
        }
    return result


TALENT_LEVELS = (10, 15, 20, 25)


def talents(aggregates: dict[str, Any], names: dict[int, str]) -> list[dict[str, Any]]:
    """Talent pick share within each tier (left vs right) and win rate."""
    stats = aggregates.get("heroStats") or {}
    hero = (aggregates.get("constants") or {}).get("hero") or {}
    counts = {t["abilityId"]: t for t in stats.get("talent") or []}
    tiers: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for t in sorted(hero.get("talents") or [], key=lambda t: t["slot"]):
        row = counts.get(t["abilityId"], {"matchCount": 0, "winCount": 0})
        tiers[TALENT_LEVELS[t["slot"] // 2]].append(
            {"ability_id": t["abilityId"], "n": row["matchCount"], "w": row["winCount"]}
        )
    result = []
    for level, options in sorted(tiers.items()):
        tier_total = sum(o["n"] for o in options)
        for o in options:
            name = names.get(o["ability_id"])
            rate = Rate(o["n"], o["w"])
            result.append(
                {
                    "level": level,
                    "talent": name or f"ability_{o['ability_id']}",
                    "name": ability_display(name, o["ability_id"]),
                    "matches": o["n"],
                    "pick_share": round(o["n"] / tier_total, 4) if tier_total else None,
                    "win_rate": round(rate.win_rate, 4) if rate.win_rate is not None else None,
                    "win_rate_ci": [round(x, 4) for x in rate.ci],
                }
            )
    return result


def basic_abilities(aggregates: dict[str, Any]) -> dict[int, str]:
    """Ability id -> internal name for the hero's learnable non-hidden abilities."""
    hero = (aggregates.get("constants") or {}).get("hero") or {}
    return {
        a["abilityId"]: (a.get("ability") or {}).get("name")
        for a in hero.get("abilities") or []
        if (a.get("ability") or {}).get("name") not in (None, "generic_hidden")
    }


def max_level_stats(aggregates: dict[str, Any]) -> list[dict[str, Any]]:
    """Per ability: hero level at which it was maxed, and win rate by that level."""
    abilities = basic_abilities(aggregates)
    rows = (aggregates.get("heroStats") or {}).get("abilityMaxLevel") or []
    by_ability: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for r in rows:
        if r["abilityId"] in abilities:
            by_ability[r["abilityId"]].append((r["level"], r["matchCount"], r["winCount"]))
    result = []
    for ability_id, levels in by_ability.items():
        name = abilities[ability_id]
        n = sum(m for _, m, _ in levels)
        median = weighted_median([(lvl, m) for lvl, m, _ in levels])
        result.append(
            {
                "ability": name,
                "name": ability_display(name, ability_id),
                "matches_maxed": n,
                "median_level_maxed": median,
                "by_level_maxed": [
                    {
                        "level": lvl,
                        "matches": m,
                        "win_rate": round(w / m, 4),
                        "win_rate_ci": [round(x, 4) for x in wilson_interval(w, m)],
                    }
                    for lvl, m, w in sorted(levels)
                    if m > 0
                ],
            }
        )
    return sorted(result, key=lambda r: r["median_level_maxed"] or 99)


def skill_orders(
    guides: Sequence[GuideMatch], aggregates: dict[str, Any], names: dict[int, str]
) -> dict[str, Any]:
    """Most common first-N skill sequences and max orders in guide matches."""
    abilities = basic_abilities(aggregates)

    def label(ability_id: int) -> str:
        name = abilities.get(ability_id) or names.get(ability_id)
        return ability_display(name, ability_id)

    firsts: Counter[tuple[str, ...]] = Counter()
    max_orders: Counter[tuple[str, ...]] = Counter()
    for g in guides:
        learned = g.ability_ids[:SKILL_ORDER_LEVELS]
        if len(learned) == SKILL_ORDER_LEVELS:
            firsts[tuple(label(a) for a in learned)] += 1
        # Order in which the non-ultimate basic abilities got their 4th point.
        points: Counter[int] = Counter()
        order: list[str] = []
        for a in g.ability_ids:
            if a not in abilities:
                continue
            points[a] += 1
            if points[a] == 4:
                order.append(label(a))
        if order:
            max_orders[tuple(order)] += 1
    n = len(guides)
    return {
        "guide_matches": n,
        "first_levels": [
            {"order": list(seq), "matches": c, "share": round(c / n, 4)}
            for seq, c in firsts.most_common(3)
        ],
        "max_order": [
            {"order": list(seq), "matches": c, "share": round(c / n, 4)}
            for seq, c in max_orders.most_common(3)
        ],
    }


def facet_choices(guides: Sequence[GuideMatch], aggregates: dict[str, Any]) -> list[dict]:
    """Facet picks in guide matches. STRATZ ``variant`` is the 1-based facet slot; 0 = none."""
    hero = (aggregates.get("constants") or {}).get("hero") or {}
    by_slot = {f["slot"] + 1: f["facetId"] for f in hero.get("facets") or []}
    counts = Counter(g.variant for g in guides if g.variant)
    n = len(guides)
    return [
        {"variant": v, "facet_id": by_slot.get(v), "matches": c, "share": round(c / n, 4)}
        for v, c in counts.most_common()
    ]


def popularity_fallback(raw: dict[str, dict[str, int]]) -> dict[str, list[dict[str, Any]]]:
    """OpenDota itemPopularity: item counts per game phase, most bought first."""
    result = {}
    for phase in ("start_game_items", "early_game_items", "mid_game_items", "late_game_items"):
        counts = raw.get(phase) or {}
        rows = []
        for item_id, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            name, display = item_name(int(item_id))
            rows.append({"item_id": int(item_id), "item": name, "name": display, "count": count})
        result[phase.removesuffix("_items")] = rows
    return result


def neutral_rows(
    raw: Iterable[tuple[int, int, int]], tiers: dict[int, int], tier: int | None
) -> list[dict[str, Any]]:
    """(item id, games, wins) -> rows with tier, usage share within tier and win rate."""
    rows = [(i, g, w, tiers.get(i)) for i, g, w in raw if g > 0]
    if tier is not None:
        rows = [r for r in rows if r[3] == tier]
    tier_totals: Counter[int | None] = Counter()
    for _, g, _, t in rows:
        tier_totals[t] += g
    result = []
    for item_id, games, wins, t in rows:
        name, display = item_name(item_id)
        rate = Rate(games, wins)
        result.append(
            {
                "item_id": item_id,
                "item": name,
                "name": display,
                "tier": t,
                "matches": games,
                "share_of_tier": round(games / tier_totals[t], 4) if tier_totals[t] else None,
                "win_rate": round(rate.win_rate, 4),
                "win_rate_ci": [round(x, 4) for x in rate.ci],
            }
        )
    return sorted(result, key=lambda r: (r["tier"] or 99, -r["matches"]))
