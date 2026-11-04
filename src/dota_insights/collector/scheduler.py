"""In-process scheduler: an asyncio loop that runs due jobs one at a time.

A job is due when its last run started at least ``interval`` ago (a failed run is retried
after ``RETRY_AFTER`` instead). Runs are recorded in ``collector_job_runs``, so a restarted
container picks up where it left off instead of re-running everything. Jobs run sequentially:
the budget and the sources' rate limits are shared, and nothing gains from parallelism.

Run a single scheduler per database; two would both pass the budget checks.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import select, update

from dota_insights.collector.budget import BudgetExceededError
from dota_insights.collector.context import CollectorContext
from dota_insights.collector.db import as_utc
from dota_insights.collector.jobs import JOBS, JobResult, JobSpec
from dota_insights.db.models import CollectorJobRun

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(minutes=5)
DEFAULT_TICK_SECONDS = 30.0


async def run_job(ctx: CollectorContext, spec: JobSpec) -> JobResult:
    """Run ``spec`` once and record it; errors are recorded and logged, not raised."""
    started = ctx.clock()
    calls_before = sum(ctx.calls.values())
    with ctx.session_factory.begin() as session:
        run = CollectorJobRun(job=spec.name, started_at=started, status="running")
        session.add(run)
        session.flush()
        run_id = run.run_id
    try:
        result = await spec.func(ctx)
    except BudgetExceededError as exc:
        result = JobResult(status="skipped", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - one failing job must not stop the scheduler
        log.exception("collector job failed", extra={"job": spec.name})
        result = JobResult(status="failed", detail=f"{type(exc).__name__}: {exc}"[:500])
    with ctx.session_factory.begin() as session:
        session.execute(
            update(CollectorJobRun)
            .where(CollectorJobRun.run_id == run_id)
            .values(
                finished_at=ctx.clock(),
                status=result.status,
                api_calls=sum(ctx.calls.values()) - calls_before,
                rows=result.rows,
                detail=(result.detail or "")[:500] or None,
            )
        )
    log.info(
        "collector job finished",
        extra={"job": spec.name, "status": result.status, "rows": result.rows},
    )
    return result


def is_due(ctx: CollectorContext, spec: JobSpec) -> bool:
    with ctx.session_factory() as session:
        last = session.execute(
            select(CollectorJobRun.started_at, CollectorJobRun.status)
            .where(CollectorJobRun.job == spec.name)
            .order_by(CollectorJobRun.started_at.desc())
            .limit(1)
        ).first()
    if last is None:
        return True
    wait = RETRY_AFTER if last.status == "failed" else spec.interval
    return ctx.clock() - as_utc(last.started_at) >= min(wait, spec.interval)


async def run_due(ctx: CollectorContext, specs: Iterable[JobSpec]) -> list[str]:
    """Run every due job in priority order; return the names that ran."""
    ran = []
    for spec in specs:
        if is_due(ctx, spec):
            await run_job(ctx, spec)
            ran.append(spec.name)
    return ran


async def run_forever(
    ctx: CollectorContext,
    specs: Iterable[JobSpec] | None = None,
    *,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    specs = list(JOBS.values() if specs is None else specs)
    stop = stop or asyncio.Event()
    log.info("collector scheduler started", extra={"jobs": [s.name for s in specs]})
    while not stop.is_set():
        await run_due(ctx, specs)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
