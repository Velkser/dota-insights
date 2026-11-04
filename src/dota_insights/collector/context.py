"""What a collector job gets: sessions, budgeted API clients and a clock."""

import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from dota_insights.collector.budget import OPENDOTA, STRATZ, ApiBudget
from dota_insights.collector.db import utc_now
from dota_insights.config import settings
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient


@dataclass
class CollectorContext:
    session_factory: sessionmaker[Session]
    budget: ApiBudget
    opendota: OpenDotaClient
    stratz: StratzClient | None = None
    clock: Callable[[], datetime] = utc_now
    calls: Counter[str] = field(default_factory=Counter)
    # How often this job runs, for budget pacing; None = the job's own interval.
    pace_interval: timedelta | None = None
    # time.monotonic() value after which jobs stop starting new API calls or batches.
    deadline: float | None = None

    def every(self, default: timedelta) -> timedelta:
        return self.pace_interval or default

    def out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    async def call_opendota(self, request: Callable[[OpenDotaClient], Awaitable[Any]]) -> Any:
        """Reserve one OpenDota call from the budget, then make it."""
        self.budget.reserve(OPENDOTA)
        self.calls[OPENDOTA] += 1
        return await request(self.opendota)

    async def call_stratz(self, document: str) -> Any:
        if self.stratz is None:
            raise RuntimeError("STRATZ token not configured")
        self.budget.reserve(STRATZ)
        self.calls[STRATZ] += 1
        return await self.stratz.query(document)


@asynccontextmanager
async def collector_context(
    session_factory: sessionmaker[Session] | None = None,
) -> AsyncIterator[CollectorContext]:
    """A context with clients rate-limited to the collector's share of each source.

    Retries are kept low: the budget counts one call per request, and every retry the client
    makes is an uncounted call against the provider's quota.
    """
    if session_factory is None:
        from dota_insights.db.session import get_sessionmaker

        session_factory = get_sessionmaker()
    opendota = OpenDotaClient(
        requests_per_minute=settings.collector_opendota_per_minute, max_retries=2
    )
    stratz = (
        StratzClient(requests_per_minute=settings.collector_stratz_per_minute, max_retries=2)
        if settings.stratz_api_token
        else None
    )
    try:
        yield CollectorContext(session_factory, ApiBudget(session_factory), opendota, stratz)
    finally:
        await opendota.aclose()
        if stratz is not None:
            await stratz.aclose()
