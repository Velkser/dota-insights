"""Collector jobs (p2-01). Each is an async function of a ``CollectorContext``.

- ``patches``: OpenDota ``/constants/patch`` (1 call) -> ``patches``; re-labels matches, marks
  stale aggregates and invalidates the server's shared Redis cache when the list changes.
- ``constants``: OpenDota ``/constants/{resource}`` (6 calls) -> ``reference_snapshots``.
- ``hero-stats``: OpenDota ``/heroStats`` (1 call) -> ``hero_stats_daily``.
- ``public-matches``: OpenDota ``/explorer`` on ``public_matches`` per rank bracket (paced; one
  ``/publicMatches`` page to seed a stream) -> draft-level matches, ``collector_cursors``.
- ``backfill``: OpenDota ``/explorer`` on ``public_matches`` (paced) -> draft-level matches in gaps.
- ``enrich``: STRATZ aliased ``match(id)`` batches (paced) -> player stats, positions, purchases.
- ``aggregate``: no API calls -> every aggregate table.
- ``usage``: no API calls -> moves the server's Redis usage queue into ``usage_events`` /
  ``usage_feedback`` and deletes rows past their retention (``dota_insights.usage.store``).
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update

from dota_insights.cache import invalidate_shared
from dota_insights.collector.aggregates import run_aggregation
from dota_insights.collector.budget import OPENDOTA, STRATZ, BudgetExceededError
from dota_insights.collector.context import CollectorContext
from dota_insights.collector.db import as_utc, upsert
from dota_insights.collector.ingest import apply_details, store_summaries, stratz_matches_query
from dota_insights.collector.patches import ensure_patches, sync_patches
from dota_insights.config import settings
from dota_insights.db.models import (
    CollectorCursor,
    CollectorGap,
    HeroStatsDaily,
    Match,
    ReferenceSnapshot,
)

log = logging.getLogger(__name__)

CONSTANT_RESOURCES = ("heroes", "items", "patch", "game_mode", "lobby_type", "region")
EXPLORER_COLUMNS = (
    "match_id, start_time, duration, lobby_type, game_mode, avg_rank_tier, radiant_win, "
    "radiant_team, dire_team"
)
# Ranked matches are enriched once STRATZ has had time to parse them, and not after they age out.
# A lookup that finds no parsed match is retried when the match is older: attempt n (0-based)
# waits for ENRICH_MIN_AGE[n]; after the last one the match stays draft-only.
ENRICH_MIN_AGE = (timedelta(hours=1), timedelta(hours=3), timedelta(hours=12))
ENRICH_MAX_AGE = timedelta(days=14)

# Forward ingest only passes matches that started this long ago (games still running then are
# rare), and re-seeds a stream that fell further behind than MAX_LAG.
SETTLE = timedelta(hours=2)
MAX_LAG = timedelta(hours=6)

PUBLIC_MATCHES_INTERVAL = timedelta(minutes=10)
BACKFILL_INTERVAL = timedelta(minutes=30)
ENRICH_INTERVAL = timedelta(minutes=15)


@dataclass
class JobResult:
    rows: int = 0
    detail: str | None = None
    status: str = "ok"


JobFunc = Callable[[CollectorContext], Awaitable[JobResult]]


@dataclass(frozen=True)
class JobSpec:
    name: str
    func: JobFunc
    interval: timedelta
    sources: tuple[str, ...]
    description: str


def _stream(bracket: int) -> str:
    return f"public_matches:{bracket}"


def _bracket_of_stream(stream: str) -> int:
    return int(stream.rsplit(":", 1)[1])


def _rank_range(bracket: int) -> tuple[int, int]:
    return bracket * 10, bracket * 10 + 9


async def job_patches(ctx: CollectorContext) -> JobResult:
    raw = await ctx.call_opendota(lambda c: c.constants("patch"))
    with ctx.session_factory.begin() as session:
        changed = sync_patches(session, raw)
    if changed:
        # Cached tool answers in Redis were computed for the old patch list.
        await invalidate_shared(f"patches changed: {changed}")
    return JobResult(rows=len(changed), detail=f"changed patches: {changed}" if changed else None)


async def job_constants(ctx: CollectorContext) -> JobResult:
    stored = 0
    for resource in CONSTANT_RESOURCES:
        payload = await ctx.call_opendota(lambda c, r=resource: c.constants(r))
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with ctx.session_factory.begin() as session:
            exists = session.get(ReferenceSnapshot, (resource, digest))
            if exists is None:
                session.add(
                    ReferenceSnapshot(
                        resource=resource, sha256=digest, fetched_at=ctx.clock(), payload=payload
                    )
                )
                stored += 1
            if resource == "patch":
                sync_patches(session, payload)
    return JobResult(rows=stored)


def hero_stats_rows(raw: list[dict], day) -> list[dict]:
    rows = []
    for hero in raw:
        hero_id = int(hero["id"])
        for bracket in range(1, 9):
            rows.append(
                {
                    "day": day,
                    "scope": "pub",
                    "rank_bracket": bracket,
                    "hero_id": hero_id,
                    "picks": int(hero.get(f"{bracket}_pick") or 0),
                    "wins": int(hero.get(f"{bracket}_win") or 0),
                    "bans": None,
                }
            )
        rows.append(
            {
                "day": day,
                "scope": "pro",
                "rank_bracket": 0,
                "hero_id": hero_id,
                "picks": int(hero.get("pro_pick") or 0),
                "wins": int(hero.get("pro_win") or 0),
                "bans": int(hero.get("pro_ban") or 0),
            }
        )
        rows.append(
            {
                "day": day,
                "scope": "turbo",
                "rank_bracket": 0,
                "hero_id": hero_id,
                "picks": int(hero.get("turbo_picks") or 0),
                "wins": int(hero.get("turbo_wins") or 0),
                "bans": None,
            }
        )
    return rows


async def job_hero_stats(ctx: CollectorContext) -> JobResult:
    raw = await ctx.call_opendota(lambda c: c.hero_stats())
    rows = hero_stats_rows(raw, ctx.clock().date())
    with ctx.session_factory.begin() as session:
        upsert(
            session,
            HeroStatsDaily.__table__,
            rows,
            ("day", "scope", "rank_bracket", "hero_id"),
            replace=("picks", "wins", "bans"),
        )
    return JobResult(rows=len(rows))


def explorer_sql(
    bracket: int,
    *,
    after_id: int,
    before_id: int | None = None,
    started_before: datetime | None = None,
    limit: int,
    ascending: bool,
) -> str:
    """Explorer SQL for ranked matches of one bracket in an exclusive match id range.

    Explorer answers ``match_id`` ranges in well under a second; ``start_time`` alone times out,
    but as an extra filter on an id range it is cheap. All values are formatted from ints.
    """
    min_rank, max_rank = _rank_range(bracket)
    where = [f"match_id > {int(after_id)}"]
    if before_id is not None:
        where.append(f"match_id < {int(before_id)}")
    if started_before is not None:
        where.append(f"start_time < {int(started_before.timestamp())}")
    where += ["lobby_type = 7", f"avg_rank_tier >= {min_rank}", f"avg_rank_tier <= {max_rank}"]
    order = "ASC" if ascending else "DESC"
    return (
        f"SELECT {EXPLORER_COLUMNS} FROM public_matches WHERE {' AND '.join(where)} "
        f"ORDER BY match_id {order} LIMIT {int(limit)}"
    )


def _newest(rows: list[dict]) -> tuple[int, datetime]:
    row = max(rows, key=lambda r: int(r["match_id"]))
    return int(row["match_id"]), datetime.fromtimestamp(int(row["start_time"]), UTC)


async def job_public_matches(ctx: CollectorContext) -> JobResult:
    """Read new ranked matches per bracket from Explorer, ascending from each stream's cursor.

    Match ids are assigned at game start but rows appear when games end, so the cursor only
    passes matches that started ``SETTLE`` ago: a long game is not skipped by shorter games that
    started after it. A stream without a cursor, or one more than ``MAX_LAG`` behind (after an
    outage), is (re)seeded from one ``publicMatches`` page; a skipped id range becomes a gap.
    """
    brackets = list(settings.collector_rank_brackets)
    allowance = min(
        ctx.budget.pace(OPENDOTA, ctx.every(PUBLIC_MATCHES_INTERVAL)),
        settings.collector_max_pages_per_run * len(brackets),
    )
    if allowance <= 0:
        return JobResult(status="skipped", detail="no OpenDota budget left today")
    limit = settings.collector_explorer_rows_per_call
    now = ctx.clock()
    with ctx.session_factory() as session:
        cursors = {c.stream: c for c in session.scalars(select(CollectorCursor))}

    def last_update(bracket: int) -> float:
        cursor = cursors.get(_stream(bracket))
        return as_utc(cursor.updated_at).timestamp() if cursor else 0.0

    # Least recently updated brackets first, so a small allowance rotates through all of them.
    brackets.sort(key=last_update)
    per_bracket = max(1, allowance // len(brackets))
    spent = new_matches = gaps = 0
    for bracket in brackets:
        if spent >= allowance or ctx.out_of_time():
            break
        stream = _stream(bracket)
        cursor = cursors.get(stream)
        position = (cursor.newest_match_id, as_utc(cursor.newest_start_time)) if cursor else None
        calls = 0
        if position is None or position[1] < now - MAX_LAG:
            min_rank, max_rank = _rank_range(bracket)
            page = await ctx.call_opendota(
                lambda c, lo=min_rank, hi=max_rank: c.public_matches(min_rank=lo, max_rank=hi)
            )
            spent, calls = spent + 1, calls + 1
            if not page:
                continue
            # Matches on the page have just ended; resume just below the oldest of them.
            oldest = min(page, key=lambda r: int(r["match_id"]))
            seed = (int(oldest["match_id"]) - 1, datetime.fromtimestamp(oldest["start_time"], UTC))
            with ctx.session_factory.begin() as session:
                new_matches += len(store_summaries(session, page))
                if position is not None and position[0] < seed[0]:
                    session.add(
                        CollectorGap(
                            stream=stream,
                            low_match_id=position[0],
                            high_match_id=seed[0] + 1,
                            created_at=now,
                        )
                    )
                    gaps += 1
            position = seed
        while calls < per_bracket and spent < allowance and not ctx.out_of_time():
            sql = explorer_sql(
                bracket,
                after_id=position[0],
                started_before=now - SETTLE,
                limit=limit,
                ascending=True,
            )
            rows = _explorer_rows(await ctx.call_opendota(lambda c, q=sql: c.explorer(q)))
            spent, calls = spent + 1, calls + 1
            with ctx.session_factory.begin() as session:
                new_matches += len(store_summaries(session, rows))
            if rows:
                position = _newest(rows)
            if len(rows) < limit:
                break
        with ctx.session_factory.begin() as session:
            upsert(
                session,
                CollectorCursor.__table__,
                [
                    {
                        "stream": stream,
                        "newest_match_id": position[0],
                        "newest_start_time": position[1],
                        "updated_at": now,
                    }
                ],
                ("stream",),
                replace=("newest_match_id", "newest_start_time", "updated_at"),
            )
    return JobResult(rows=new_matches, detail=f"calls={spent} new_gaps={gaps}")


def _explorer_rows(result: dict) -> list[dict]:
    if result.get("err"):
        raise RuntimeError(f"explorer error: {str(result['err'])[:200]}")
    return result.get("rows") or []


async def job_backfill(ctx: CollectorContext) -> JobResult:
    """Fill recorded gaps from OpenDota Explorer, newest gap first (one call per chunk).

    Explorer filters on ``match_id`` ranges fast (``start_time`` filters time out), and one call
    returns up to ``collector_explorer_rows_per_call`` rows instead of publicMatches' 100.
    """
    allowance = ctx.budget.pace(OPENDOTA, ctx.every(BACKFILL_INTERVAL))
    limit = settings.collector_explorer_rows_per_call
    with ctx.session_factory() as session:
        gaps = list(
            session.scalars(
                select(CollectorGap)
                .where(CollectorGap.filled_at.is_(None))
                .order_by(CollectorGap.high_match_id.desc())
            )
        )
    if not gaps:
        return JobResult(detail="no open gaps")
    if allowance <= 0:
        return JobResult(status="skipped", detail="no OpenDota budget left today")
    spent = stored = filled = 0
    for gap in gaps:
        bracket = _bracket_of_stream(gap.stream)
        high = gap.high_match_id
        while spent < allowance and not ctx.out_of_time():
            sql = explorer_sql(
                bracket, after_id=gap.low_match_id, before_id=high, limit=limit, ascending=False
            )
            rows = _explorer_rows(await ctx.call_opendota(lambda c, q=sql: c.explorer(q)))
            spent += 1
            with ctx.session_factory.begin() as session:
                stored += len(store_summaries(session, rows))
                done = len(rows) < limit
                if not done:
                    high = min(int(row["match_id"]) for row in rows)
                    done = high <= gap.low_match_id + 1
                session.execute(
                    update(CollectorGap)
                    .where(CollectorGap.gap_id == gap.gap_id)
                    .values(
                        high_match_id=max(high, gap.low_match_id + 1),
                        filled_at=ctx.clock() if done else None,
                    )
                )
            if done:
                filled += 1
                break
        if spent >= allowance or ctx.out_of_time():
            break
    return JobResult(rows=stored, detail=f"calls={spent} gaps_filled={filled}")


async def job_enrich(ctx: CollectorContext) -> JobResult:
    """Fetch player stats for recent draft-level matches from STRATZ, a batch per call."""
    if ctx.stratz is None:
        return JobResult(status="skipped", detail="DOTA_INSIGHTS_STRATZ_API_TOKEN not set")
    allowance = ctx.budget.pace(STRATZ, ctx.every(ENRICH_INTERVAL))
    if allowance <= 0:
        return JobResult(status="skipped", detail="no STRATZ budget left")
    per_call = settings.collector_enrich_matches_per_call
    now = ctx.clock()
    with ctx.session_factory() as session:
        candidates = list(
            session.execute(
                select(Match.match_id, Match.start_time, Match.patch_id)
                .where(
                    ~Match.has_details,
                    Match.start_time > now - ENRICH_MAX_AGE,
                    or_(
                        *(
                            and_(Match.detail_attempts == n, Match.start_time < now - age)
                            for n, age in enumerate(ENRICH_MIN_AGE)
                        )
                    ),
                )
                .order_by(Match.detail_attempts, Match.start_time.desc())
                .limit(allowance * per_call)
            )
        )
    enriched = missing = calls = 0
    for start in range(0, len(candidates), per_call):
        if ctx.out_of_time():
            break
        batch = candidates[start : start + per_call]
        data = await ctx.call_stratz(stratz_matches_query([m.match_id for m in batch]))
        calls += 1
        with ctx.session_factory.begin() as session:
            for match in batch:
                payload = (data or {}).get(f"m{match.match_id}")
                if apply_details(
                    session, match.match_id, match.start_time, match.patch_id, payload
                ):
                    enriched += 1
                else:
                    missing += 1
                    session.execute(
                        update(Match)
                        .where(
                            Match.match_id == match.match_id, Match.start_time == match.start_time
                        )
                        .values(detail_attempts=Match.detail_attempts + 1),
                        execution_options={"synchronize_session": False},
                    )
    return JobResult(rows=enriched, detail=f"calls={calls} not_found={missing}")


async def job_aggregate(ctx: CollectorContext) -> JobResult:
    with ctx.session_factory.begin() as session:
        ensure_patches(session)
    result = run_aggregation(
        ctx.session_factory, settings.collector_aggregate_batch_size, should_stop=ctx.out_of_time
    )
    detail = f"draft={result.draft_matches} detail={result.detail_matches}"
    if result.rebuilt_patches:
        detail += f" rebuilt={result.rebuilt_patches}"
    return JobResult(rows=result.draft_matches + result.detail_matches, detail=detail)


async def job_usage(ctx: CollectorContext) -> JobResult:
    from dota_insights.kv import KVError, get_kv
    from dota_insights.usage import store

    kv = get_kv()
    moved, detail = 0, "no Redis configured, nothing to flush"
    if kv is not None:
        try:
            moved = await store.flush_queue(kv, ctx.session_factory, should_stop=ctx.out_of_time)
            detail = f"flushed={moved}"
        except KVError:
            detail = "Redis unavailable, flush skipped"
    with ctx.session_factory.begin() as session:
        calls, feedback = store.prune(session, ctx.clock())
    return JobResult(rows=moved, detail=f"{detail} pruned_calls={calls} pruned_feedback={feedback}")


# In priority order: the scheduler runs due jobs top to bottom.
JOBS: dict[str, JobSpec] = {
    spec.name: spec
    for spec in (
        JobSpec(
            "patches", job_patches, timedelta(hours=6), (OPENDOTA,), "patch list and versioning"
        ),
        JobSpec(
            "constants", job_constants, timedelta(hours=24), (OPENDOTA,), "dotaconstants snapshots"
        ),
        JobSpec("hero-stats", job_hero_stats, timedelta(hours=24), (OPENDOTA,), "daily /heroStats"),
        JobSpec(
            "public-matches",
            job_public_matches,
            PUBLIC_MATCHES_INTERVAL,
            (OPENDOTA,),
            "newest ranked matches per bracket",
        ),
        JobSpec("backfill", job_backfill, BACKFILL_INTERVAL, (OPENDOTA,), "fill gaps via Explorer"),
        JobSpec("enrich", job_enrich, ENRICH_INTERVAL, (STRATZ,), "player stats from STRATZ"),
        JobSpec("aggregate", job_aggregate, timedelta(minutes=10), (), "incremental aggregates"),
        JobSpec("usage", job_usage, timedelta(hours=1), (), "flush usage events, apply retention"),
    )
}

__all__ = ["JOBS", "BudgetExceededError", "JobResult", "JobSpec"]
