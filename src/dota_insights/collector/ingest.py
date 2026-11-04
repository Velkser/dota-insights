"""Normalise source payloads into ``matches`` / ``match_players`` / ``match_events`` rows.

Two levels of data:

- **Draft level** from OpenDota ``publicMatches`` or Explorer ``public_matches``: match header
  plus the ten hero ids and the result. Player stats stay NULL.
- **Detail level** from STRATZ ``match(id)``: per-player stats, positions, lanes, final items
  and purchase times, written over the draft rows. ``matches.has_details`` marks it.

All writes are idempotent: matches and players are inserted with ``ON CONFLICT DO NOTHING``, so
re-reading an overlapping page neither duplicates rows nor counts a match twice in aggregates.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from dota_insights.collector.db import ensure_month_partitions, insert_new, upsert
from dota_insights.collector.patches import PatchLookup, ensure_patches, patch_windows
from dota_insights.db.models import EventType, Match, MatchEvent, MatchPlayer, MatchSource

RANKED_LOBBY = 7
MATCH_KEY = ("match_id", "start_time")
PLAYER_KEY = ("match_id", "start_time", "slot")


def bracket_of(avg_rank_tier: int | None) -> int:
    """Medal of an OpenDota rank tier: 54 (Legend 4) -> 5; 0 when unknown."""
    if not avg_rank_tier:
        return 0
    return min(8, max(0, int(avg_rank_tier) // 10))


def parse_summary(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``matches`` row (plus the two teams) from a publicMatches/Explorer row, or None to skip.

    Keeps only ranked matches that were played (duration > 0, ten real heroes).
    """
    radiant, dire = row.get("radiant_team") or [], row.get("dire_team") or []
    if (
        row.get("lobby_type") != RANKED_LOBBY
        or not row.get("duration")
        or len(radiant) != 5
        or len(dire) != 5
        or not all(radiant)
        or not all(dire)
    ):
        return None
    tier = row.get("avg_rank_tier")
    tier = round(tier) if tier is not None else None
    return {
        "match_id": int(row["match_id"]),
        "start_time": datetime.fromtimestamp(int(row["start_time"]), UTC),
        "duration": int(row["duration"]),
        "game_mode": int(row.get("game_mode") or 0),
        "lobby_type": RANKED_LOBBY,
        "avg_rank_tier": tier if tier is not None and 10 <= tier <= 85 else None,
        "radiant_win": bool(row["radiant_win"]),
        "radiant_team": [int(hero) for hero in radiant],
        "dire_team": [int(hero) for hero in dire],
    }


def store_summaries(session: Session, rows: Iterable[Mapping[str, Any]]) -> list[int]:
    """Insert draft-level matches; return the ids that were new."""
    ensure_patches(session)
    patch_of = PatchLookup(patch_windows(session))
    summaries = []
    for row in rows:
        summary = parse_summary(row)
        if summary is None:
            continue
        patch_id = patch_of(summary["start_time"])
        if patch_id is None:
            continue
        summaries.append(summary | {"patch_id": patch_id})
    if not summaries:
        return []

    ensure_month_partitions(session, (s["start_time"] for s in summaries))
    match_rows = [
        {key: s[key] for key in s if key not in ("radiant_team", "dire_team")}
        | {"source": MatchSource.OPENDOTA}
        for s in summaries
    ]
    new_ids = {key[0] for key in insert_new(session, Match.__table__, match_rows, MATCH_KEY)}
    players = []
    for s in summaries:
        if s["match_id"] not in new_ids:
            continue
        for slot, hero_id in enumerate(s["radiant_team"] + s["dire_team"]):
            is_radiant = slot < 5
            players.append(
                {
                    "match_id": s["match_id"],
                    "start_time": s["start_time"],
                    "slot": slot,
                    "patch_id": s["patch_id"],
                    "hero_id": hero_id,
                    "is_radiant": is_radiant,
                    "win": s["radiant_win"] == is_radiant,
                }
            )
    upsert(session, MatchPlayer.__table__, players, PLAYER_KEY)
    return sorted(new_ids)


# STRATZ enrichment

STRATZ_MATCH_FIELDS = """
  id didRadiantWin durationSeconds startDateTime
  pickBans { isPick heroId }
  players {
    playerSlot heroId isRadiant position lane steamAccountId
    kills deaths assists goldPerMinute experiencePerMinute numLastHits numDenies networth
    heroDamage towerDamage heroHealing
    item0Id item1Id item2Id item3Id item4Id item5Id backpack0Id backpack1Id backpack2Id neutral0Id
    stats { itemPurchases { itemId time } }
  }
"""


def stratz_matches_query(match_ids: Sequence[int]) -> str:
    """One GraphQL document fetching several matches through aliases (one call against quota).

    ``matches(ids:)`` needs admin rights on a Default token; aliased ``match(id:)`` does not.
    """
    parts = [f"m{int(mid)}: match(id: {int(mid)}) {{{STRATZ_MATCH_FIELDS}}}" for mid in match_ids]
    return "query {\n" + "\n".join(parts) + "\n}"


# STRATZ lane -> OpenDota lane_role (1 safe, 2 mid, 3 off, 4 jungle).
_LANE_ROLE = {"SAFE_LANE": 1, "MID_LANE": 2, "OFF_LANE": 3, "JUNGLE": 4}


def _lane(lane_role: int | None, is_radiant: bool) -> int | None:
    """OpenDota lane (1 bot, 2 mid, 3 top, 4/5 jungle) from lane role and side."""
    if lane_role is None:
        return None
    if lane_role == 2:
        return 2
    if lane_role == 4:
        return 4 if is_radiant else 5
    safe = lane_role == 1
    return 1 if safe == is_radiant else 3


def _position(value: str | None) -> int | None:
    if value and value.startswith("POSITION_"):
        return int(value.removeprefix("POSITION_"))
    return None


def _slot(player_slot: int) -> int:
    return player_slot if player_slot < 128 else player_slot - 128 + 5


def apply_details(
    session: Session,
    match_id: int,
    start_time: datetime,
    patch_id: int,
    data: Mapping[str, Any] | None,
) -> bool:
    """Write STRATZ player stats and purchases over a draft-level match. False if unusable."""
    players = (data or {}).get("players") or []
    if len(players) != 10 or any(_position(p.get("position")) is None for p in players):
        return False
    radiant_win = bool(data["didRadiantWin"])
    rows, events = [], []
    for player in sorted(players, key=lambda p: p["playerSlot"]):
        slot = _slot(int(player["playerSlot"]))
        is_radiant = bool(player["isRadiant"])
        lane_role = _LANE_ROLE.get(player.get("lane") or "")
        rows.append(
            {
                "match_id": match_id,
                "start_time": start_time,
                "slot": slot,
                "patch_id": patch_id,
                "account_id": player.get("steamAccountId"),
                "hero_id": int(player["heroId"]),
                "is_radiant": is_radiant,
                "position": _position(player.get("position")),
                "lane": _lane(lane_role, is_radiant),
                "lane_role": lane_role,
                "kills": player["kills"],
                "deaths": player["deaths"],
                "assists": player["assists"],
                "gpm": player["goldPerMinute"],
                "xpm": player["experiencePerMinute"],
                "last_hits": player["numLastHits"],
                "denies": player["numDenies"],
                "net_worth": player.get("networth"),
                "hero_damage": player.get("heroDamage"),
                "tower_damage": player.get("towerDamage"),
                "healing": player.get("heroHealing"),
                "items": [player.get(f"item{i}Id") or 0 for i in range(6)],
                "backpack": [player.get(f"backpack{i}Id") or 0 for i in range(3)],
                "item_neutral": player.get("neutral0Id"),
                "win": radiant_win == is_radiant,
            }
        )
        for purchase in (player.get("stats") or {}).get("itemPurchases") or []:
            if purchase.get("itemId") is None or purchase.get("time") is None:
                continue
            events.append(
                {
                    "match_id": match_id,
                    "start_time": start_time,
                    "event_type": EventType.PURCHASE,
                    "time": int(purchase["time"]),
                    "slot": slot,
                    "item_id": int(purchase["itemId"]),
                    "payload": {},
                }
            )

    stat_columns = [column for column in rows[0] if column not in PLAYER_KEY]
    upsert(session, MatchPlayer.__table__, rows, PLAYER_KEY, replace=stat_columns)
    events.sort(key=lambda event: (event["time"], event["slot"]))
    for seq, event in enumerate(events):
        event["seq"] = seq
    upsert(session, MatchEvent.__table__, events, ("match_id", "start_time", "seq"))

    pick_bans = data.get("pickBans") or []
    known_bans = [pb["heroId"] for pb in pick_bans if not pb.get("isPick") and pb.get("heroId")]
    session.execute(
        update(Match)
        .where(Match.match_id == match_id, Match.start_time == start_time)
        .values(
            has_details=True,
            is_parsed=bool(events),
            banned_hero_ids=known_bans or None,
        ),
        execution_options={"synchronize_session": False},
    )
    return True
