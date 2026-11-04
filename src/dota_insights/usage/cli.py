"""``dota-insights usage ...`` and ``dota-insights privacy ...``.

- ``usage report [--days 30] [--json]``: the usage report (``usage.report``).
- ``usage flush``: move the Redis usage queue into the database now (the collector's ``usage``
  job does this hourly), then apply the retention.
- ``privacy opt-out <player> [--note TEXT]``, ``privacy opt-in <player>``, ``privacy list``:
  manage ``player_opt_outs``. ``<player>`` takes what player tools take (account id, SteamID64
  or profile URL). The shared Redis copy of the list is refreshed right away.

All commands need ``DOTA_INSIGHTS_DATABASE_URL``; flush and the opt-out refresh also use Redis.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from sqlalchemy import select

from dota_insights.analytics.players import PlayerInputError, parse_player
from dota_insights.collector.db import as_utc, utc_now
from dota_insights.db.models import PlayerOptOut
from dota_insights.logging import setup_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dota-insights", description=__doc__)
    top = parser.add_subparsers(dest="area", required=True)

    usage = top.add_parser("usage", help="usage analytics").add_subparsers(
        dest="command", required=True
    )
    report = usage.add_parser("report", help="calls, clients, success rates, feedback")
    report.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    report.add_argument("--json", action="store_true", help="print the report as JSON")
    usage.add_parser("flush", help="move queued usage events into the database now")

    privacy = top.add_parser("privacy", help="player opt-outs").add_subparsers(
        dest="command", required=True
    )
    opt_out = privacy.add_parser("opt-out", help="stop showing a player")
    opt_out.add_argument("player", help="account id, SteamID64 or profile URL")
    opt_out.add_argument("--note", help="why, or how the request arrived (max 200 chars)")
    opt_in = privacy.add_parser("opt-in", help="remove a player from the opt-out list")
    opt_in.add_argument("player", help="account id, SteamID64 or profile URL")
    privacy.add_parser("list", help="list opted-out players")
    return parser


def _report(days: int, as_json: bool) -> int:
    from dota_insights.db.session import session_scope
    from dota_insights.usage import store
    from dota_insights.usage.report import build_report, format_report

    with session_scope() as session:
        report = build_report(session, utc_now(), days)
    report["queued"] = asyncio.run(store.queue_length())
    print(json.dumps(report, indent=2, default=str) if as_json else format_report(report))
    return 0


def _flush() -> int:
    from dota_insights.db.session import get_sessionmaker, session_scope
    from dota_insights.kv import get_kv
    from dota_insights.usage import store

    kv = get_kv()
    if kv is None:
        print("Redis is not configured; nothing to flush.")
    else:
        moved = asyncio.run(store.flush_queue(kv, get_sessionmaker()))
        print(f"flushed {moved} events")
    with session_scope() as session:
        calls, feedback = store.prune(session, utc_now())
    print(f"pruned {calls} calls and {feedback} feedback rows past retention")
    return 0


def _account(player: str) -> int | None:
    try:
        return parse_player(player)
    except PlayerInputError as exc:
        print(str(exc), file=sys.stderr)
        return None


def _privacy(command: str, player: str | None, note: str | None) -> int:
    from dota_insights import privacy
    from dota_insights.db.session import session_scope

    account = _account(player) if player is not None else None
    if player is not None and account is None:
        return 2
    with session_scope() as session:
        if command == "opt-out":
            if session.get(PlayerOptOut, account) is None:
                session.add(
                    PlayerOptOut(
                        account_id=account,
                        created_at=utc_now(),
                        note=note[:200] if note else None,
                    )
                )
            print(f"account {account} opted out")
        elif command == "opt-in":
            row = session.get(PlayerOptOut, account)
            if row is not None:
                session.delete(row)
            print(f"account {account} removed from the opt-out list")
        else:
            for row in session.scalars(select(PlayerOptOut).order_by(PlayerOptOut.account_id)):
                print(
                    f"{row.account_id}  since {as_utc(row.created_at):%Y-%m-%d}  {row.note or ''}"
                )
        session.flush()
        ids = list(session.scalars(select(PlayerOptOut.account_id)))
    if command != "list":
        asyncio.run(privacy.publish(ids))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging()
    if args.area == "usage":
        if args.command == "report":
            return _report(args.days, args.json)
        return _flush()
    return _privacy(args.command, getattr(args, "player", None), getattr(args, "note", None))
