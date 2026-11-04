"""Persistent API call budgets for the collector.

Every call the collector makes is reserved here first, against per-source limits on UTC day and
hour windows stored in ``collector_api_usage``, so restarts don't reset the count. Limits are set
below the providers' quotas: the MCP server makes live OpenDota calls from the same IP and needs
the rest of the free tier.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dota_insights.collector.db import upsert, utc_now
from dota_insights.config import settings
from dota_insights.db.models import CollectorApiUsage

OPENDOTA = "opendota"
STRATZ = "stratz"

WINDOW_SECONDS = {"day": 86_400, "hour": 3_600}


class BudgetExceededError(Exception):
    """The call would go over a collector budget; the job should stop for this window."""

    def __init__(self, source: str, window: str, limit: int) -> None:
        super().__init__(f"{source} {window} budget of {limit} calls used up")
        self.source = source
        self.window = window


@dataclass(frozen=True)
class Limit:
    window: str  # "day" | "hour"
    calls: int


def default_limits() -> dict[str, tuple[Limit, ...]]:
    return {
        OPENDOTA: (Limit("day", settings.collector_opendota_daily_budget),),
        STRATZ: (
            Limit("day", settings.collector_stratz_daily_budget),
            Limit("hour", settings.collector_stratz_hourly_budget),
        ),
    }


def window_start(window: str, now: datetime) -> datetime:
    start = now.replace(minute=0, second=0, microsecond=0)
    return start.replace(hour=0) if window == "day" else start


class ApiBudget:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        limits: dict[str, tuple[Limit, ...]] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self.limits = default_limits() if limits is None else limits
        self._clock = clock

    def _used(self, session: Session, source: str, window: str, now: datetime) -> int:
        calls = session.scalar(
            select(CollectorApiUsage.calls).where(
                CollectorApiUsage.source == source,
                CollectorApiUsage.window == window,
                CollectorApiUsage.window_start == window_start(window, now),
            )
        )
        return calls or 0

    def remaining(self, source: str) -> int:
        """Calls left in the tightest window for ``source``."""
        now = self._clock()
        with self._session_factory() as session:
            return min(
                (
                    limit.calls - self._used(session, source, limit.window, now)
                    for limit in self.limits[source]
                ),
                default=0,
            )

    def reserve(self, source: str, calls: int = 1) -> None:
        """Count ``calls`` against every window, or raise ``BudgetExceededError`` and count none."""
        now = self._clock()
        with self._session_factory.begin() as session:
            for limit in self.limits[source]:
                if self._used(session, source, limit.window, now) + calls > limit.calls:
                    raise BudgetExceededError(source, limit.window, limit.calls)
            rows = [
                {
                    "source": source,
                    "window": limit.window,
                    "window_start": window_start(limit.window, now),
                    "calls": calls,
                }
                for limit in self.limits[source]
            ]
            upsert(
                session,
                CollectorApiUsage.__table__,
                rows,
                ("source", "window", "window_start"),
                add=("calls",),
            )

    def pace(self, source: str, interval: timedelta) -> int:
        """Calls one run may spend so that runs every ``interval`` spread the budget evenly.

        For each window: what is left divided by the runs still to come before the window resets.
        A run always gets at least one call while any budget is left.
        """
        now = self._clock()
        allowance = None
        with self._session_factory() as session:
            for limit in self.limits[source]:
                left = limit.calls - self._used(session, source, limit.window, now)
                if left <= 0:
                    return 0
                reset = window_start(limit.window, now) + timedelta(
                    seconds=WINDOW_SECONDS[limit.window]
                )
                runs_left = max(1, math.ceil((reset - now) / interval))
                share = max(1, left // runs_left)
                allowance = share if allowance is None else min(allowance, share)
        return allowance or 0
