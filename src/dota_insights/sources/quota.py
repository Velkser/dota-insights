"""Server-wide caps on source requests, shared by every serverless instance through Redis.

Each instance's ``TokenBucket`` only paces its own requests; the providers count all of ours
together (OpenDota per egress IP, STRATZ per token). ``SharedQuota`` keeps a per-minute and a
per-UTC-day counter in Redis: a request over the minute cap waits for the next minute, one over
the daily cap fails with the source's rate-limit error and the seconds until midnight UTC.
Without Redis (or while it fails) the counters live in this instance's memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from dota_insights import metrics
from dota_insights.kv import KVError, get_kv

logger = logging.getLogger(__name__)

DAY = 86400
MINUTE = 60

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class QuotaUsage:
    source: str
    minute_used: int
    minute_limit: int
    day_used: int
    day_limit: int

    @property
    def day_left(self) -> int:
        return max(0, self.day_limit - self.day_used)


class QuotaExhaustedError(Exception):
    """Raised by ``SharedQuota.acquire`` when today's cap is spent (before mapping)."""

    def __init__(self, source: str, retry_after: float) -> None:
        super().__init__(f"{source} daily quota for this server is spent")
        self.source = source
        self.retry_after = retry_after


# Alerts hook: called with the usage after each counted request; see ``telegram.alerts``.
QuotaObserver = Callable[[QuotaUsage], Awaitable[None]]
_observers: list[QuotaObserver] = []


def observe(callback: QuotaObserver) -> None:
    _observers.append(callback)


class SharedQuota:
    """Per-minute and per-day request caps for one source across all instances."""

    def __init__(
        self,
        source: str,
        *,
        per_minute: int,
        per_day: int,
        clock: Clock = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.source = source
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._sleep = sleep
        self._local: dict[str, int] = {}
        self.last_usage: QuotaUsage | None = None

    def _keys(self, now: float) -> tuple[str, str]:
        return (
            f"di:quota:{self.source}:m:{int(now // MINUTE)}",
            f"di:quota:{self.source}:d:{int(now // DAY)}",
        )

    async def _count(self, now: float) -> tuple[int, int]:
        minute_key, day_key = self._keys(now)
        kv = get_kv()
        if kv is not None:
            try:
                minute, day = await kv.incr_windows([(minute_key, 2 * MINUTE), (day_key, 2 * DAY)])
                return minute, day
            except KVError:
                pass
        # Drop counters of windows that have passed.
        for key in [k for k in self._local if k not in (minute_key, day_key)]:
            del self._local[key]
        self._local[minute_key] = self._local.get(minute_key, 0) + 1
        self._local[day_key] = self._local.get(day_key, 0) + 1
        return self._local[minute_key], self._local[day_key]

    async def acquire(self) -> QuotaUsage:
        """Count a request; wait out a full minute, raise ``QuotaExhaustedError`` on a full day."""
        while True:
            now = self._clock()
            minute, day = await self._count(now)
            usage = QuotaUsage(self.source, minute, self.per_minute, day, self.per_day)
            self.last_usage = usage
            metrics.record_quota(self.source, usage.day_left)
            for observer in _observers:
                try:
                    await observer(usage)
                except Exception:
                    logger.exception("quota observer failed")
            if day > self.per_day:
                raise QuotaExhaustedError(self.source, DAY - now % DAY)
            if minute <= self.per_minute:
                return usage
            await self._sleep(MINUTE - now % MINUTE + 0.05)

    async def usage(self) -> QuotaUsage:
        """Current counts without spending a request (for ``/status``)."""
        now = self._clock()
        minute_key, day_key = self._keys(now)
        kv = get_kv()
        values: list[str | None] | None = None
        if kv is not None:
            try:
                values = await kv.mget([minute_key, day_key])
            except KVError:
                values = None
        if values is None:
            minute, day = self._local.get(minute_key, 0), self._local.get(day_key, 0)
        else:
            minute, day = (int(v) if v else 0 for v in values)
        return QuotaUsage(self.source, minute, self.per_minute, day, self.per_day)
