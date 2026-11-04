"""Single-match analysis over OpenDota ``/matches/{id}``.

Every ``*_summary`` function takes the raw match JSON and returns plain dicts, so each number
can be traced to a field of the source response. Only the Steam API fields (KDA, GPM, XPM, net
worth, damage, healing, final items, draft) exist for every match; per-minute series, events,
teamfights, wards and lane data exist only once OpenDota has parsed the replay. ``load_match``
fetches a match and requests a parse when it is missing.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from dota_insights.cache import TTL_MATCH, cached
from dota_insights.reference import hero_by_id, item_by_id, items
from dota_insights.sources import get_opendota
from dota_insights.sources.opendota import OpenDotaError, OpenDotaNotFoundError

RADIANT, DIRE = "radiant", "dire"

# A parse request costs 10 OpenDota rate-limit units; ask at most once per match per window.
PARSE_REQUEST_TTL = 5 * 60

# Biggest contributors: weights of a player's share of their team's totals.
CONTRIBUTION_WEIGHTS = {
    "kill_participation": 0.30,
    "hero_damage": 0.25,
    "net_worth": 0.15,
    "tower_damage": 0.15,
    "hero_healing": 0.15,
}
_TEAM_TOTALS = ("hero_damage", "net_worth", "tower_damage", "hero_healing")
# The winner is flagged as a comeback when it was behind by at least this much gold.
COMEBACK_THRESHOLD = 5000
LANING_MINUTE = 10
# Lane outcome: a side wins the lane when its lane heroes' gold + XP at 10:00 exceeds the
# other side's by at least this share of the larger total.
LANE_WIN_MARGIN = 0.10
TURNING_POINT_WINDOW = 3  # minutes
CONSUMABLES = frozenset(
    {
        "tango",
        "tango_single",
        "flask",
        "clarity",
        "enchanted_mango",
        "faerie_fire",
        "tpscroll",
        "ward_observer",
        "ward_sentry",
        "ward_dispenser",
        "dust",
        "smoke_of_deceit",
        "blood_grenade",
        "famango",
        "great_famango",
        "greater_famango",
    }
)
_LANES = {1: "bottom", 2: "middle", 3: "top"}
_LANE_ROLES = {1: "safe", 2: "mid", 3: "off", 4: "jungle"}


class MatchNotFoundError(Exception):
    """OpenDota has no match with this id."""


@dataclass(frozen=True)
class MatchData:
    """A fetched match and, for an unparsed one, what happened to the parse request."""

    raw: dict[str, Any]
    parsed: bool
    parse_requested: bool = False
    parse_error: str | None = None


class _Unparsed(Exception):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw


def is_parsed(raw: dict[str, Any]) -> bool:
    """OpenDota marks parsed matches with ``od_data.has_parsed`` and a non-null ``version``."""
    return bool((raw.get("od_data") or {}).get("has_parsed")) or raw.get("version") is not None


@cached(ttl=TTL_MATCH)
async def _fetch_parsed(match_id: int) -> dict[str, Any]:
    # Raising keeps an unparsed match out of the week-long cache, so it is re-fetched once
    # the requested parse lands.
    raw = await get_opendota().match(match_id)
    if not is_parsed(raw):
        raise _Unparsed(raw)
    return raw


@cached(ttl=PARSE_REQUEST_TTL)
async def _request_parse(match_id: int) -> dict[str, Any]:
    return await get_opendota().request_parse(match_id)


async def load_match(match_id: int) -> MatchData:
    """Fetch a match; if it is unparsed, queue a parse (at most once per 5 minutes)."""
    try:
        return MatchData(raw=await _fetch_parsed(match_id), parsed=True)
    except OpenDotaNotFoundError as exc:
        raise MatchNotFoundError(match_id) from exc
    except _Unparsed as unparsed:
        raw = unparsed.raw
    try:
        await _request_parse(match_id)
    except OpenDotaError as exc:
        return MatchData(raw=raw, parsed=False, parse_error=str(exc))
    return MatchData(raw=raw, parsed=False, parse_requested=True)


# Helpers


def side_of(player_slot: int) -> str:
    return RADIANT if player_slot < 128 else DIRE


def other(side: str) -> str:
    return DIRE if side == RADIANT else RADIANT


def hero_name(hero_id: int | None) -> str | None:
    hero = hero_by_id().get(hero_id) if hero_id is not None else None
    return hero.display_name if hero else None


def _hero_name_by_internal() -> dict[str, str]:
    return {h.name: h.display_name for h in hero_by_id().values()}


@functools.cache
def _item_names() -> dict[str, str]:
    return {item.name: item.display_name or item.name for item in items()}


def _item_name(key: str) -> str:
    return _item_names().get(key, key)


def clock(seconds: int | float | None) -> str | None:
    """``-89`` -> ``"-1:29"``, ``3900`` -> ``"65:00"``."""
    if seconds is None:
        return None
    sign = "-" if seconds < 0 else ""
    total = int(abs(seconds))
    return f"{sign}{total // 60}:{total % 60:02d}"


def match_time(raw: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """(start, end) of the match in UTC."""
    start = raw.get("start_time")
    if start is None:
        return None, None
    began = datetime.fromtimestamp(start, UTC)
    end = datetime.fromtimestamp(start + (raw.get("duration") or 0), UTC)
    return began, end


def _players(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(raw.get("players") or [], key=lambda p: p["player_slot"])


def _ident(player: dict[str, Any]) -> dict[str, Any]:
    return {
        "player_slot": player["player_slot"],
        "side": side_of(player["player_slot"]),
        "hero": hero_name(player.get("hero_id")),
        "hero_id": player.get("hero_id"),
    }


def _ratio(part: float, total: float) -> float:
    return part / total if total else 0.0


def _at(series: list[int] | None, minute: int) -> int | None:
    if not series:
        return None
    return series[minute] if minute < len(series) else series[-1]


# analyze_match


def basics(raw: dict[str, Any]) -> dict[str, Any]:
    """Result, duration and score: available for every match."""
    winner = RADIANT if raw.get("radiant_win") else DIRE
    start, _ = match_time(raw)
    return {
        "match_id": raw.get("match_id"),
        "winner": winner,
        "duration_s": raw.get("duration"),
        "duration": clock(raw.get("duration")),
        "score": {RADIANT: raw.get("radiant_score"), DIRE: raw.get("dire_score")},
        "start_time": start.isoformat() if start else None,
        "game_mode": raw.get("game_mode"),
        "lobby_type": raw.get("lobby_type"),
        "patch": raw.get("patch"),
        "region": raw.get("region"),
        "first_blood_time": clock(raw.get("first_blood_time")),
        "parsed": is_parsed(raw),
    }


def draft(raw: dict[str, Any]) -> list[dict[str, Any]]:
    picks_bans = sorted(raw.get("picks_bans") or [], key=lambda pb: pb.get("order", 0))
    return [
        {
            "order": pb.get("order"),
            "action": "pick" if pb.get("is_pick") else "ban",
            "side": RADIANT if pb.get("team") == 0 else DIRE,
            "hero": hero_name(pb.get("hero_id")),
        }
        for pb in picks_bans
    ]


def player_lines(raw: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    for p in _players(raw):
        kills, deaths, assists = p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0)
        lines.append(
            {
                **_ident(p),
                "account_id": p.get("account_id"),
                "rank_tier": p.get("rank_tier"),
                "level": p.get("level"),
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "kda": round((kills + assists) / max(1, deaths), 2),
                "gpm": p.get("gold_per_min"),
                "xpm": p.get("xp_per_min"),
                "net_worth": p.get("net_worth"),
                "last_hits": p.get("last_hits"),
                "denies": p.get("denies"),
                "hero_damage": p.get("hero_damage"),
                "tower_damage": p.get("tower_damage"),
                "hero_healing": p.get("hero_healing"),
                "stuns_s": round(p["stuns"], 1) if p.get("stuns") is not None else None,
                "final_items": [
                    _final_item(p.get(f"item_{i}")) for i in range(6) if p.get(f"item_{i}")
                ],
            }
        )
    return lines


def _final_item(item_id: int) -> str:
    item = item_by_id().get(item_id)
    return (item.display_name or item.name) if item else str(item_id)


def contributors(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every player's contribution score, highest first (see ``CONTRIBUTION_WEIGHTS``)."""
    team: dict[str, dict[str, float]] = {}
    for side in (RADIANT, DIRE):
        mine = [line for line in lines if line["side"] == side]
        team[side] = {
            "kills": sum(line["kills"] for line in mine),
            **{m: sum(line[m] or 0 for line in mine) for m in _TEAM_TOTALS},
        }
    scored = []
    for line in lines:
        totals = team[line["side"]]
        shares = {
            "kill_participation": _ratio(line["kills"] + line["assists"], totals["kills"]),
            "hero_damage": _ratio(line["hero_damage"] or 0, totals["hero_damage"]),
            "net_worth": _ratio(line["net_worth"] or 0, totals["net_worth"]),
            "tower_damage": _ratio(line["tower_damage"] or 0, totals["tower_damage"]),
            "hero_healing": _ratio(line["hero_healing"] or 0, totals["hero_healing"]),
        }
        score = sum(CONTRIBUTION_WEIGHTS[k] * v for k, v in shares.items())
        scored.append(
            {
                "player_slot": line["player_slot"],
                "side": line["side"],
                "hero": line["hero"],
                "score": round(score, 3),
                "shares": {k: round(v, 3) for k, v in shares.items()},
            }
        )
    return sorted(scored, key=lambda s: s["score"], reverse=True)


def advantage_swing(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Largest leads per side and the comeback flag, from ``radiant_gold_adv`` (parsed only)."""
    adv = raw.get("radiant_gold_adv") or []
    if not adv:
        return None
    max_rad = max(range(len(adv)), key=lambda i: adv[i])
    max_dire = min(range(len(adv)), key=lambda i: adv[i])
    winner = RADIANT if raw.get("radiant_win") else DIRE
    deficit = max(0, -adv[max_dire]) if winner == RADIANT else max(0, adv[max_rad])
    return {
        "max_radiant_lead": {"gold": max(0, adv[max_rad]), "minute": max_rad},
        "max_dire_lead": {"gold": max(0, -adv[max_dire]), "minute": max_dire},
        "final_radiant_gold_adv": adv[-1],
        "swing_range": adv[max_rad] - adv[max_dire],
        "winner_max_deficit": deficit,
        "comeback": deficit >= COMEBACK_THRESHOLD,
    }


def match_summary(raw: dict[str, Any]) -> dict[str, Any]:
    lines = player_lines(raw)
    ranked = contributors(lines)
    return {
        **basics(raw),
        "draft": draft(raw),
        "players": lines,
        "biggest_contributors": {
            "overall": ranked[:3],
            RADIANT: next((c for c in ranked if c["side"] == RADIANT), None),
            DIRE: next((c for c in ranked if c["side"] == DIRE), None),
        },
        "gold_swing": advantage_swing(raw),
    }


# get_match_timeline


def _building(key: str) -> tuple[str, str]:
    """``npc_dota_badguys_tower2_mid`` -> (side that lost it, readable name)."""
    owner = RADIANT if "goodguys" in key else DIRE
    name = key.split("guys_", 1)[-1]
    if name.startswith("tower"):
        kind = "tower"
    elif "rax" in name:
        kind = "barracks"
    elif name == "fort":
        kind = "ancient"
    else:
        kind = "building"
    return owner, f"{kind}:{name}"


def _slot_hero(raw: dict[str, Any]) -> dict[int, str | None]:
    return {p["player_slot"]: hero_name(p.get("hero_id")) for p in raw.get("players") or []}


def events(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Key objectives and buybacks, in time order (parsed only)."""
    heroes = _slot_hero(raw)
    internal = _hero_name_by_internal()
    out: list[dict[str, Any]] = []
    for o in raw.get("objectives") or []:
        kind, t = o.get("type"), o.get("time")
        slot = o.get("player_slot")
        if kind == "CHAT_MESSAGE_FIRSTBLOOD":
            victim = o.get("victim_player_slot")
            if victim is not None and victim not in heroes and 5 <= victim <= 9:
                victim = _team_slot_to_player_slot(victim)
            out.append(
                {
                    "type": "first_blood",
                    "side": side_of(slot) if slot is not None else None,
                    "hero": heroes.get(slot),
                    "victim": heroes.get(victim),
                    "time_s": t,
                }
            )
        elif kind == "building_kill":
            owner, name = _building(o.get("key", ""))
            out.append(
                {
                    "type": name.split(":")[0],
                    "building": name.split(":")[1],
                    "lost_by": owner,
                    "destroyed_by": heroes.get(slot)
                    or internal.get(o.get("unit", ""))
                    or (
                        "creeps"
                        if "creep" in o.get("unit", "") or "siege" in o.get("unit", "")
                        else o.get("unit")
                    ),
                    "time_s": t,
                }
            )
        elif kind == "CHAT_MESSAGE_ROSHAN_KILL":
            out.append({"type": "roshan", "side": _team_side(o.get("team")), "time_s": t})
        elif kind == "CHAT_MESSAGE_AEGIS":
            out.append(
                {"type": "aegis", "side": side_of(slot), "hero": heroes.get(slot), "time_s": t}
            )
        elif kind == "CHAT_MESSAGE_AEGIS_STOLEN":
            out.append(
                {
                    "type": "aegis_stolen",
                    "side": side_of(slot),
                    "hero": heroes.get(slot),
                    "time_s": t,
                }
            )
        elif kind == "CHAT_MESSAGE_DENIED_AEGIS":
            out.append({"type": "aegis_denied", "hero": heroes.get(slot), "time_s": t})
        elif kind == "CHAT_MESSAGE_MINIBOSS_KILL":
            out.append(
                {
                    "type": "tormentor",
                    "side": _team_side(o.get("team")),
                    "hero": heroes.get(slot),
                    "time_s": t,
                }
            )
    for p in raw.get("players") or []:
        for b in p.get("buyback_log") or []:
            out.append(
                {
                    "type": "buyback",
                    "side": side_of(p["player_slot"]),
                    "hero": heroes.get(p["player_slot"]),
                    "time_s": b.get("time"),
                }
            )
    out.sort(key=lambda e: e["time_s"] if e["time_s"] is not None else 0)
    for e in out:
        e["time"] = clock(e["time_s"])
    return out


def _team_side(team: int | None) -> str | None:
    return {2: RADIANT, 3: DIRE}.get(team) if team is not None else None


def _team_slot_to_player_slot(slot: int) -> int:
    """OpenDota's ``slot`` (0-9) to ``player_slot`` (0-4, 128-132)."""
    return slot if slot < 5 else 128 + slot - 5


def advantage_series(raw: dict[str, Any]) -> list[dict[str, int]]:
    gold = raw.get("radiant_gold_adv") or []
    xp = raw.get("radiant_xp_adv") or []
    return [
        {"minute": i, "gold_adv": g, "xp_adv": xp[i] if i < len(xp) else None}
        for i, g in enumerate(gold)
    ]


def purchases(raw: dict[str, Any], *, include_consumables: bool = False) -> list[dict[str, Any]]:
    out = []
    for p in _players(raw):
        log = [
            {"time": clock(e["time"]), "time_s": e["time"], "item": _item_name(e["key"])}
            for e in p.get("purchase_log") or []
            if include_consumables or e["key"] not in CONSUMABLES
        ]
        out.append({**_ident(p), "purchases": log})
    return out


def timeline_summary(raw: dict[str, Any], *, include_consumables: bool = False) -> dict[str, Any]:
    return {
        **basics(raw),
        "advantage_by_minute": advantage_series(raw),
        "events": events(raw),
        "item_purchases": purchases(raw, include_consumables=include_consumables),
    }


# analyze_teamfights


def _fight_side_totals(raw: dict[str, Any], fight: dict[str, Any]) -> dict[str, dict[str, int]]:
    totals = {s: {"deaths": 0, "gold_delta": 0, "xp_delta": 0} for s in (RADIANT, DIRE)}
    for slot, fp in enumerate(fight.get("players") or []):
        side = RADIANT if slot < 5 else DIRE
        totals[side]["deaths"] += fp.get("deaths", 0)
        totals[side]["gold_delta"] += fp.get("gold_delta", 0)
        totals[side]["xp_delta"] += fp.get("xp_delta", 0)
    return totals


def _fight_winner(totals: dict[str, dict[str, int]]) -> str | None:
    rad, dire = totals[RADIANT]["gold_delta"], totals[DIRE]["gold_delta"]
    if rad == dire:
        return None
    return RADIANT if rad > dire else DIRE


def teamfight_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    players = _players(raw)
    fights = []
    for n, fight in enumerate(raw.get("teamfights") or [], start=1):
        totals = _fight_side_totals(raw, fight)
        participants = []
        for slot, fp in enumerate(fight.get("players") or []):
            took_part = fp.get("damage", 0) or fp.get("healing", 0) or fp.get("deaths", 0)
            if not took_part or slot >= len(players):
                continue
            participants.append(
                {
                    **_ident(players[slot]),
                    "damage": fp.get("damage", 0),
                    "healing": fp.get("healing", 0),
                    "deaths": fp.get("deaths", 0),
                    "kills": sum((fp.get("killed") or {}).values()),
                    "buybacks": fp.get("buybacks", 0),
                    "gold_delta": fp.get("gold_delta", 0),
                    "xp_delta": fp.get("xp_delta", 0),
                }
            )
        fights.append(
            {
                "fight": n,
                "start": clock(fight.get("start")),
                "end": clock(fight.get("end")),
                "start_s": fight.get("start"),
                "end_s": fight.get("end"),
                "deaths": fight.get("deaths"),
                "deaths_by_side": {s: totals[s]["deaths"] for s in totals},
                "gold_swing_radiant": totals[RADIANT]["gold_delta"] - totals[DIRE]["gold_delta"],
                "xp_swing_radiant": totals[RADIANT]["xp_delta"] - totals[DIRE]["xp_delta"],
                "winner": _fight_winner(totals),
                "participants": participants,
            }
        )
    return fights


def teamfight_summary(raw: dict[str, Any]) -> dict[str, Any]:
    fights = teamfight_list(raw)
    won = {s: sum(1 for f in fights if f["winner"] == s) for s in (RADIANT, DIRE)}
    stuns = [
        {**_ident(p), "stuns_s": round(p["stuns"], 1)}
        for p in _players(raw)
        if p.get("stuns") is not None
    ]
    return {
        **basics(raw),
        "fight_count": len(fights),
        "fights_won": won,
        "teamfights": fights,
        "match_stuns": stuns,
    }


# find_turning_points


def turning_points(
    raw: dict[str, Any], *, window: int = TURNING_POINT_WINDOW, top: int = 5
) -> list[dict[str, Any]]:
    """Largest gold-advantage changes over ``window`` minutes, without overlaps."""
    gold = raw.get("radiant_gold_adv") or []
    xp = raw.get("radiant_xp_adv") or []
    if len(gold) <= window:
        return []
    candidates = sorted(
        ((gold[i + window] - gold[i], i) for i in range(len(gold) - window)),
        key=lambda c: abs(c[0]),
        reverse=True,
    )
    chosen: list[tuple[int, int]] = []
    for change, start in candidates:
        if change == 0 or len(chosen) >= top:
            break
        if all(abs(start - s) >= window for _, s in chosen):
            chosen.append((change, start))
    fights = teamfight_list(raw)
    objectives = [e for e in events(raw) if e["type"] not in ("first_blood",)]
    out = []
    for change, start in sorted(chosen, key=lambda c: c[1]):
        lo, hi = start * 60, (start + window) * 60
        out.append(
            {
                "from_minute": start,
                "to_minute": start + window,
                "gold_adv_change_radiant": change,
                "xp_adv_change_radiant": (
                    xp[start + window] - xp[start] if len(xp) > start + window else None
                ),
                "gold_adv_before": gold[start],
                "gold_adv_after": gold[start + window],
                "benefited": RADIANT if change > 0 else DIRE,
                "teamfights": [
                    {
                        "fight": f["fight"],
                        "start": f["start"],
                        "deaths_by_side": f["deaths_by_side"],
                        "gold_swing_radiant": f["gold_swing_radiant"],
                        "winner": f["winner"],
                    }
                    for f in fights
                    if f["end_s"] is not None and f["end_s"] >= lo and f["start_s"] <= hi
                ],
                "objectives": [e for e in objectives if lo <= (e["time_s"] or 0) <= hi],
            }
        )
    return out


# analyze_vision


def _ward_log(p: dict[str, Any], kind: str, team_heroes: set[str]) -> tuple[list[int], int, int]:
    """(lifetimes of wards that left the map, destroyed by the enemy, still up at the end)."""
    placed = {w.get("ehandle"): w["time"] for w in p.get(f"{kind}_log") or []}
    lifetimes, destroyed, gone = [], 0, set()
    for w in p.get(f"{kind}_left_log") or []:
        start = placed.get(w.get("ehandle"))
        if start is None:
            continue
        gone.add(w.get("ehandle"))
        lifetimes.append(w["time"] - start)
        if w.get("attackername") not in team_heroes:
            destroyed += 1
    return lifetimes, destroyed, len(placed) - len(gone)


def vision_summary(raw: dict[str, Any]) -> dict[str, Any]:
    side_heroes: dict[str, set[str]] = {RADIANT: set(), DIRE: set()}
    internal = {h.id: h.name for h in hero_by_id().values()}
    for p in raw.get("players") or []:
        side_heroes[side_of(p["player_slot"])].add(internal.get(p.get("hero_id"), ""))
    players = []
    for p in _players(raw):
        side = side_of(p["player_slot"])
        row: dict[str, Any] = _ident(p)
        for kind, label in (("obs", "observers"), ("sen", "sentries")):
            lifetimes, destroyed, alive = _ward_log(p, kind, side_heroes[side])
            row[label] = {
                "placed": len(p.get(f"{kind}_log") or []),
                "destroyed_by_enemy": destroyed,
                "up_at_game_end": alive,
                "avg_lifetime_s": round(sum(lifetimes) / len(lifetimes)) if lifetimes else None,
            }
        row["dewards"] = {
            "observers": p.get("observer_kills") or 0,
            "sentries": p.get("sentry_kills") or 0,
        }
        players.append(row)
    teams = {}
    for side in (RADIANT, DIRE):
        mine = [r for r in players if r["side"] == side]
        teams[side] = {
            "observers_placed": sum(r["observers"]["placed"] for r in mine),
            "sentries_placed": sum(r["sentries"]["placed"] for r in mine),
            "observers_lost": sum(r["observers"]["destroyed_by_enemy"] for r in mine),
            "sentries_lost": sum(r["sentries"]["destroyed_by_enemy"] for r in mine),
            "dewards": sum(r["dewards"]["observers"] + r["dewards"]["sentries"] for r in mine),
        }
    n = len(players) or 1
    averages = {
        "observers_placed": round(sum(r["observers"]["placed"] for r in players) / n, 2),
        "sentries_placed": round(sum(r["sentries"]["placed"] for r in players) / n, 2),
        "dewards": round(
            sum(r["dewards"]["observers"] + r["dewards"]["sentries"] for r in players) / n, 2
        ),
    }
    for r in players:
        r["vs_match_average"] = {
            "observers_placed": round(r["observers"]["placed"] - averages["observers_placed"], 2),
            "sentries_placed": round(r["sentries"]["placed"] - averages["sentries_placed"], 2),
            "dewards": round(
                r["dewards"]["observers"] + r["dewards"]["sentries"] - averages["dewards"], 2
            ),
        }
    return {**basics(raw), "teams": teams, "match_average_per_player": averages, "players": players}


# analyze_laning_phase


def laning_summary(raw: dict[str, Any], minute: int = LANING_MINUTE) -> dict[str, Any]:
    players = _players(raw)
    internal = {h.name: h.id for h in hero_by_id().values()}
    rows = []
    for p in players:
        rows.append(
            {
                **_ident(p),
                "lane": _LANES.get(p.get("lane")),
                "lane_role": _LANE_ROLES.get(p.get("lane_role")),
                "is_roaming": p.get("is_roaming"),
                "lane_efficiency_pct": p.get("lane_efficiency_pct"),
                f"last_hits_{minute}": _at(p.get("lh_t"), minute),
                f"denies_{minute}": _at(p.get("dn_t"), minute),
                f"gold_{minute}": _at(p.get("gold_t"), minute),
                f"xp_{minute}": _at(p.get("xp_t"), minute),
            }
        )
    lanes = []
    for lane in ("top", "middle", "bottom"):
        sides = {}
        for side in (RADIANT, DIRE):
            mine = [
                r for r in rows if r["lane"] == lane and r["side"] == side and not r["is_roaming"]
            ]
            sides[side] = {
                "heroes": [r["hero"] for r in mine],
                "gold": sum(r[f"gold_{minute}"] or 0 for r in mine),
                "xp": sum(r[f"xp_{minute}"] or 0 for r in mine),
            }
        rad = sides[RADIANT]["gold"] + sides[RADIANT]["xp"]
        dire = sides[DIRE]["gold"] + sides[DIRE]["xp"]
        margin = _ratio(rad - dire, max(rad, dire))
        if abs(margin) < LANE_WIN_MARGIN:
            outcome = "even"
        else:
            outcome = f"{RADIANT if margin > 0 else DIRE}_won"
        lanes.append(
            {"lane": lane, **sides, "radiant_margin": round(margin, 3), "outcome": outcome}
        )
    lane_of = {p["hero_id"]: _LANES.get(p.get("lane")) for p in players}
    early = []
    for p in players:
        for k in p.get("kills_log") or []:
            if k["time"] > minute * 60:
                continue
            victim_id = internal.get(k["key"])
            killer_lane, victim_lane = lane_of.get(p.get("hero_id")), lane_of.get(victim_id)
            early.append(
                {
                    "time": clock(k["time"]),
                    "time_s": k["time"],
                    "killer": hero_name(p.get("hero_id")),
                    "killer_side": side_of(p["player_slot"]),
                    "victim": hero_name(victim_id),
                    "killer_lane": killer_lane,
                    "victim_lane": victim_lane,
                    "rotation": killer_lane is not None and killer_lane != victim_lane,
                }
            )
    early.sort(key=lambda e: e["time_s"])
    return {**basics(raw), "minute": minute, "players": rows, "lanes": lanes, "early_kills": early}
