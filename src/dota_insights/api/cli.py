"""``dota-insights api-keys ...``: manage Developer API keys.

- ``api-keys create <name> [--per-minute N] [--per-day N]``: create a key and print it. The
  plain key is shown only this once; the database keeps its HMAC-SHA256.
- ``api-keys list``: every key with its id, prefix, quotas and whether it is revoked.
- ``api-keys revoke <id>``: revoke a key. Server instances stop accepting it within a minute.

Needs ``DOTA_INSIGHTS_DATABASE_URL`` and ``DOTA_INSIGHTS_API_KEY_SECRET`` (the same secret as
the server); revoke also writes the shared Redis cache when Redis is configured.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from dota_insights.collector.db import as_utc
from dota_insights.config import settings
from dota_insights.logging import setup_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dota-insights api-keys", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a key and print it once")
    create.add_argument("name", help="who or what the key is for (max 100 chars)")
    create.add_argument(
        "--per-minute",
        type=int,
        help=f"calls a minute (default {settings.api_key_per_minute}, from settings)",
    )
    create.add_argument(
        "--per-day", type=int, help=f"calls a day (default {settings.api_key_per_day})"
    )
    commands.add_parser("list", help="list keys")
    revoke = commands.add_parser("revoke", help="revoke a key")
    revoke.add_argument("id", help="the key's id, from `api-keys list`")
    return parser


def _quota(value: int | None, default: int) -> str:
    return str(value) if value is not None else f"{default} (default)"


def main(argv: Sequence[str] | None = None) -> int:
    from dota_insights.api import keys
    from dota_insights.db.session import session_scope

    args = _parser().parse_args(argv)
    setup_logging()
    if not settings.api_key_secret:
        print("DOTA_INSIGHTS_API_KEY_SECRET is not set.", file=sys.stderr)
        return 2
    with session_scope() as session:
        if args.command == "create":
            row, key = keys.create_key(
                session, args.name, per_minute=args.per_minute, per_day=args.per_day
            )
            print(f"created key {row.id} for {row.name!r}")
            print(f"  per minute: {_quota(row.per_minute, settings.api_key_per_minute)}")
            print(f"  per day:    {_quota(row.per_day, settings.api_key_per_day)}")
            print("API key (shown only now, store it safely):")
            print(key)
            return 0
        if args.command == "list":
            for row in keys.list_keys(session):
                state = f"revoked {as_utc(row.revoked_at):%Y-%m-%d}" if row.revoked_at else "active"
                print(
                    f"{row.id}  {row.prefix}…  {state:18}  "
                    f"{_quota(row.per_minute, settings.api_key_per_minute)}/min  "
                    f"{_quota(row.per_day, settings.api_key_per_day)}/day  "
                    f"since {as_utc(row.created_at):%Y-%m-%d}  {row.name}"
                )
            return 0
        row = keys.revoke_key(session, args.id)
        if row is None:
            print(f"no key with id {args.id!r}", file=sys.stderr)
            return 1
        record, key_hash = keys.record_of(row), row.key_hash
    asyncio.run(keys.publish(key_hash, record))
    print(f"key {args.id} revoked")
    return 0
