"""Player privacy (p4-10): the opt-out list, and masking players who are on it.

Players can ask not to be shown (``dota-insights privacy opt-out <account>``, stored in
``player_opt_outs``). Player tools refuse them with an ``error``; match tools keep the match but
replace an opted-out player's account id and names with ``None``. Private profiles are refused
by the player analytics themselves (``analytics.players.load_player``), following OpenDota's
flags: no public profile, or "Expose Public Match Data" off.

The list is read from the database at most once a minute per instance, shared through Redis
for ten minutes; the CLI refreshes the shared copy, so an opt-out applies within about a minute.
When the database cannot be read, the last list this instance saw is kept.
"""

import contextlib
import json
import logging
import time
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dota_insights.db.access import read, read_engine
from dota_insights.db.models import PlayerOptOut
from dota_insights.kv import KVError, get_kv

log = logging.getLogger(__name__)

SHARED_KEY = "di:privacy:opt_outs"
SHARED_TTL = 600
LOCAL_TTL = 60.0
# The account id Dota reports for players who hide their match data.
ANONYMOUS_ACCOUNT = 4294967295

OPTED_OUT_MESSAGE = (
    "Account {account} has asked not to be shown by this server, so no statistics are given."
)
OPTED_OUT_HINT = (
    "This is the player's choice and retrying will not change it. Questions about heroes, "
    "items or matches in general still work."
)

_cache: tuple[float, frozenset[int]] | None = None


def _all_opt_outs(session: Session) -> list[int]:
    return list(session.scalars(select(PlayerOptOut.account_id)))


async def _load() -> frozenset[int] | None:
    kv = get_kv()
    if kv is not None:
        try:
            shared = await kv.get(SHARED_KEY)
            if shared is not None:
                return frozenset(json.loads(shared))
        except (KVError, ValueError):
            pass
    if read_engine() is None:
        return frozenset()
    ids = await read(_all_opt_outs)
    if ids is None:
        return None
    if kv is not None:
        with contextlib.suppress(KVError):
            await kv.set(SHARED_KEY, json.dumps(sorted(ids)), ex=SHARED_TTL)
    return frozenset(ids)


async def opted_out() -> frozenset[int]:
    """Account ids on the opt-out list (empty without a database)."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < LOCAL_TTL:
        return _cache[1]
    loaded = await _load()
    if loaded is None:
        if _cache is not None:
            # Keep the last list seen, and retry after the next interval.
            _cache = (now, _cache[1])
            return _cache[1]
        return frozenset()
    _cache = (now, loaded)
    return loaded


async def is_opted_out(account_id: int) -> bool:
    return int(account_id) in await opted_out()


def forget_cache() -> None:
    """Drop this instance's copy of the list (tests, and the CLI after a change)."""
    global _cache
    _cache = None


async def publish(ids: Iterable[int]) -> None:
    """Replace the shared Redis copy of the list (the CLI calls this after a change)."""
    forget_cache()
    kv = get_kv()
    if kv is None:
        return
    try:
        await kv.set(SHARED_KEY, json.dumps(sorted(set(ids))), ex=SHARED_TTL)
    except KVError:
        log.warning("could not refresh the shared opt-out list; instances catch up in 10 min")


NAME_FIELDS = ("personaname", "name", "persona_name")


def masked(player: dict[str, Any], hidden: frozenset[int]) -> dict[str, Any]:
    """``player`` with its account id and names cleared if opted out or anonymous."""
    account = player.get("account_id")
    if account is None or not (account == ANONYMOUS_ACCOUNT or account in hidden):
        return player
    return {
        **player,
        "account_id": None,
        **{name: None for name in NAME_FIELDS if name in player},
    }


def mask_players(players: Iterable[dict[str, Any]], hidden: frozenset[int]) -> list[dict]:
    """Copies of ``players`` with opted-out and anonymous players masked."""
    return [masked(p, hidden) for p in players]
