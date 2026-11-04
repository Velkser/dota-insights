"""``dota-insights insights weekly``: write the Weekly Meta Report and insight drafts.

The reported week is the seven days before ``--end`` (default: today, UTC), compared with the
seven days before that. The weekly workflow (``.github/workflows/insights.yml``) runs it on
Mondays, so the week is Monday to Sunday. Needs ``DOTA_INSIGHTS_DATABASE_URL``.
"""

import argparse
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from dota_insights.collector.db import utc_now
from dota_insights.insights import meta
from dota_insights.insights.data import SOURCES, load_week
from dota_insights.insights.report import write_report
from dota_insights.logging import setup_logging

WEEK = timedelta(days=7)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dota-insights insights", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    weekly = sub.add_parser("weekly", help="Weekly Meta Report and anomaly drafts")
    weekly.add_argument("--out", type=Path, default=Path("reports"), help="parent folder")
    weekly.add_argument(
        "--end",
        type=date.fromisoformat,
        help="first day after the reported week, YYYY-MM-DD (default: today, UTC)",
    )
    weekly.add_argument("--source", choices=SOURCES, default="matches")
    weekly.add_argument("--min-games", type=int, default=meta.DEFAULT_MIN_GAMES)
    weekly.add_argument("--top", type=int, default=meta.DEFAULT_TOP, help="heroes per section")
    weekly.add_argument("--alpha", type=float, default=meta.DEFAULT_ALPHA, help="FDR level")
    weekly.add_argument(
        "--min-wr-delta",
        type=float,
        default=meta.DEFAULT_MIN_WR_DELTA,
        help="smallest win-rate change for an anomaly, as a fraction (0.015 = 1.5 points)",
    )
    weekly.add_argument(
        "--min-pr-delta",
        type=float,
        default=meta.DEFAULT_MIN_PR_DELTA,
        help="smallest pick-rate change for an anomaly, as a fraction",
    )
    weekly.add_argument("--max-anomalies", type=int, default=meta.DEFAULT_MAX_ANOMALIES)
    weekly.add_argument("--no-charts", action="store_true", help="skip PNG rendering")
    return parser


def weekly(args: argparse.Namespace, session_factory=None) -> int:
    if session_factory is None:
        from dota_insights.db.session import get_sessionmaker

        session_factory = get_sessionmaker()
    end = args.end or utc_now().date()
    with session_factory() as session:
        after = load_week(session, args.source, end - WEEK, end)
        before = load_week(session, args.source, end - 2 * WEEK, end - WEEK)
    settings = {
        "alpha": args.alpha,
        "min_wr_delta": args.min_wr_delta,
        "min_pr_delta": args.min_pr_delta,
    }
    report = meta.weekly_report(
        before,
        after,
        min_games=args.min_games,
        top=args.top,
        max_anomalies=args.max_anomalies,
        **settings,
    )
    written = write_report(report, args.out, settings=settings, charts=not args.no_charts)
    print(
        f"{written.folder}: {after.total_matches} matches this week, "
        f"{before.total_matches} the week before, {len(report.changes)} hero x rank changes, "
        f"{len(report.anomalies)} anomalies, {len(written.files)} files"
    )
    for error in written.chart_errors:
        print(f"chart skipped: {error}")
    if not report.changes:
        print(f"warning: no hero has {args.min_games} games in both weeks; the drafts say so")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging()
    return weekly(args)
