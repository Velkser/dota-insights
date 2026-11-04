"""``dota-insights collect ...``: run collector jobs once, run the scheduler, or show status."""

import argparse
import asyncio
import signal
import sys
from collections.abc import Sequence

from sqlalchemy import func, select

from dota_insights.collector.budget import OPENDOTA, STRATZ, ApiBudget
from dota_insights.collector.context import collector_context
from dota_insights.collector.jobs import JOBS
from dota_insights.collector.scheduler import DEFAULT_TICK_SECONDS, run_forever, run_job
from dota_insights.db.models import AggregateStatus, CollectorGap, CollectorJobRun
from dota_insights.logging import setup_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dota-insights collect", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, spec in JOBS.items():
        sub.add_parser(name, help=f"run the {name} job once: {spec.description}")
    scheduler = sub.add_parser("scheduler", help="run due jobs forever (the collector service)")
    scheduler.add_argument("--tick", type=float, default=DEFAULT_TICK_SECONDS, help="seconds")
    scheduler.add_argument("--jobs", help="comma-separated subset of jobs")
    sub.add_parser("status", help="budgets, last runs, stale patches, open gaps")
    sub.add_parser("list", help="list jobs")
    gap = sub.add_parser("add-gap", help="queue a match id range for the backfill job")
    gap.add_argument("--bracket", type=int, required=True)
    gap.add_argument("--low", type=int, required=True, help="exclusive lower match id")
    gap.add_argument("--high", type=int, required=True, help="exclusive upper match id")
    return parser


async def _run_once(name: str) -> int:
    async with collector_context() as ctx:
        result = await run_job(ctx, JOBS[name])
    print(f"{name}: {result.status} rows={result.rows} {result.detail or ''}".rstrip())
    return 1 if result.status == "failed" else 0


async def _scheduler(tick: float, names: str | None) -> int:
    specs = [JOBS[n.strip()] for n in names.split(",")] if names else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with collector_context() as ctx:
        await run_forever(ctx, specs, tick_seconds=tick, stop=stop)
    return 0


def _status() -> int:
    from dota_insights.db.session import get_sessionmaker

    factory = get_sessionmaker()
    budget = ApiBudget(factory)
    for source in (OPENDOTA, STRATZ):
        print(f"budget {source}: {budget.remaining(source)} calls left in the tightest window")
    with factory() as session:
        for name in JOBS:
            run = session.scalars(
                select(CollectorJobRun)
                .where(CollectorJobRun.job == name)
                .order_by(CollectorJobRun.started_at.desc())
                .limit(1)
            ).first()
            if run is None:
                print(f"job {name}: never run")
            else:
                print(
                    f"job {name}: {run.status} at {run.started_at:%Y-%m-%d %H:%M} "
                    f"calls={run.api_calls} rows={run.rows} {run.detail or ''}".rstrip()
                )
        stale = session.scalars(select(AggregateStatus.patch_id).where(AggregateStatus.stale))
        print(f"stale patches: {list(stale)}")
        gaps = session.scalar(
            select(func.count()).select_from(CollectorGap).where(CollectorGap.filled_at.is_(None))
        )
        print(f"open gaps: {gaps}")
    return 0


def _add_gap(bracket: int, low: int, high: int) -> int:
    from dota_insights.collector.db import utc_now
    from dota_insights.db.session import session_scope

    if low >= high:
        print("--low must be below --high", file=sys.stderr)
        return 2
    with session_scope() as session:
        session.add(
            CollectorGap(
                stream=f"public_matches:{bracket}",
                low_match_id=low,
                high_match_id=high,
                created_at=utc_now(),
            )
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging()
    if args.command == "list":
        for name, spec in JOBS.items():
            print(f"{name:15} every {spec.interval}  {spec.description}")
        return 0
    if args.command == "status":
        return _status()
    if args.command == "add-gap":
        return _add_gap(args.bracket, args.low, args.high)
    if args.command == "scheduler":
        return asyncio.run(_scheduler(args.tick, args.jobs))
    return asyncio.run(_run_once(args.command))
