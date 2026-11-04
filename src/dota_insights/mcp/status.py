"""``/status``: source quota usage, shared-state backend and data freshness, as JSON.

Unlike ``/health`` (liveness only, no I/O), this reads Redis and, when ``DATABASE_URL`` is set,
the collector's tables: the last run of each job and how far the ingest streams have read. The
database part is cached for ``FRESHNESS_TTL`` seconds and bounded by a timeout, so polling the
route cannot load Neon. Nothing secret (tokens, URLs, client addresses) is included.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dota_insights import __version__, kv
from dota_insights.config import settings
from dota_insights.sources import server_quotas
from dota_insights.telegram.alerts import get_alerter

logger = logging.getLogger(__name__)

FRESHNESS_TTL = 30.0
FRESHNESS_TIMEOUT = 5.0

_freshness: tuple[float, dict[str, Any]] | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def collector_freshness(factory: sessionmaker[Session] | None = None) -> dict[str, Any]:
    """Last run per collector job, ingest progress and aggregate staleness (sync, hits the DB)."""
    from dota_insights.db.models import AggregateStatus, CollectorCursor, CollectorJobRun
    from dota_insights.db.session import get_sessionmaker

    with (factory or get_sessionmaker())() as session:
        latest = (
            select(CollectorJobRun.job, func.max(CollectorJobRun.started_at).label("started_at"))
            .group_by(CollectorJobRun.job)
            .subquery()
        )
        runs = session.execute(
            select(CollectorJobRun).join(
                latest,
                (CollectorJobRun.job == latest.c.job)
                & (CollectorJobRun.started_at == latest.c.started_at),
            )
        ).scalars()
        last_ok = dict(
            session.execute(
                select(CollectorJobRun.job, func.max(CollectorJobRun.finished_at))
                .where(CollectorJobRun.status == "ok")
                .group_by(CollectorJobRun.job)
            ).all()
        )
        jobs = {
            run.job: {
                "status": run.status,
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "last_ok_at": _iso(last_ok.get(run.job)),
                "api_calls": run.api_calls,
                "rows": run.rows,
            }
            for run in runs
        }
        newest_match = session.scalar(select(func.max(CollectorCursor.newest_start_time)))
        stale = session.scalar(
            select(func.count()).select_from(AggregateStatus).where(AggregateStatus.stale)
        )
        rebuilt = session.scalar(select(func.max(AggregateStatus.rebuilt_at)))
    return {
        "jobs": jobs,
        "newest_match_start": _iso(newest_match),
        "aggregates": {"stale_patches": stale or 0, "last_rebuilt_at": _iso(rebuilt)},
    }


async def _data_freshness() -> dict[str, Any]:
    global _freshness
    if not settings.database_url:
        return {"configured": False}
    now = time.monotonic()
    if _freshness is not None and now - _freshness[0] < FRESHNESS_TTL:
        return _freshness[1]
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(collector_freshness), timeout=FRESHNESS_TIMEOUT
        )
        result = {"configured": True, **result}
    except Exception as exc:
        logger.warning("status: database unavailable", extra={"error": type(exc).__name__})
        result = {"configured": True, "error": f"database unavailable ({type(exc).__name__})"}
    _freshness = (now, result)
    return result


async def status_payload() -> dict[str, Any]:
    quotas = {}
    for name, quota in server_quotas().items():
        usage = await quota.usage()
        quotas[name] = {
            "minute_used": usage.minute_used,
            "minute_limit": usage.minute_limit,
            "day_used": usage.day_used,
            "day_limit": usage.day_limit,
            "day_left": usage.day_left,
            "day_resets": "00:00 UTC",
        }
    return {
        "status": "ok",
        "version": __version__,
        "shared_state": kv.backend(),
        "quotas": quotas,
        # Without Redis the counts above are this instance's only.
        "quotas_scope": "all instances" if kv.backend() == "redis" else "this instance",
        "rate_limits": {
            "enabled": settings.rate_limit_enabled,
            "per_minute": settings.rate_limit_per_minute,
            "per_day": settings.rate_limit_per_day,
            "heavy_per_minute": settings.rate_limit_heavy_per_minute,
            "heavy_per_day": settings.rate_limit_heavy_per_day,
        },
        "alerts": {"telegram": get_alerter() is not None},
        "data": await _data_freshness(),
    }


def reset_status_cache() -> None:
    global _freshness
    _freshness = None
