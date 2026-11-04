"""Collector: ingest, incremental aggregates, patch versioning, budgets, jobs and scheduler.

Runs on SQLite (the models fall back to JSON for PostgreSQL arrays); HTTP goes through
``httpx.MockTransport`` with recorded payloads, never the live APIs.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dota_insights.collector import aggregates, jobs
from dota_insights.collector.budget import OPENDOTA, STRATZ, ApiBudget, BudgetExceededError, Limit
from dota_insights.collector.context import CollectorContext
from dota_insights.collector.ingest import apply_details, parse_summary, store_summaries
from dota_insights.collector.patches import stale_patches, sync_patches
from dota_insights.collector.scheduler import run_due
from dota_insights.db.aggregates import (
    compare_hero_patches,
    hero_patch_stats,
    percentile_from_histogram,
)
from dota_insights.db.models import (
    AggregateStatus,
    Base,
    CollectorCursor,
    CollectorGap,
    CollectorJobRun,
    HeroAggregate,
    HeroDraftAggregate,
    HeroMatchupAggregate,
    HeroStatHistogram,
    HeroStatsDaily,
    ItemTimingAggregate,
    Match,
    MatchEvent,
    MatchPlayer,
    MatchupRelation,
    PatchBracketTotal,
)
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "collector"
NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
PATCHES = [
    {"id": 59, "name": "7.40", "date": "2025-12-16T00:50:40.281Z"},
    {"id": 60, "name": "7.41", "date": "2026-03-24T00:50:59.580Z"},
]
RADIANT = [1, 2, 3, 4, 5]
DIRE = [6, 7, 8, 9, 10]


def public_row(match_id: int, start: datetime, **overrides) -> dict:
    row = {
        "match_id": match_id,
        "start_time": int(start.timestamp()),
        "duration": 2400,
        "lobby_type": 7,
        "game_mode": 22,
        "avg_rank_tier": 54,
        "radiant_win": True,
        "radiant_team": RADIANT,
        "dire_team": DIRE,
    }
    return row | overrides


@pytest.fixture
def factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'collector.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        sync_patches(session, PATCHES)
    yield factory
    engine.dispose()


def count(session: Session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where))


# Ingest


def test_parse_summary_keeps_only_played_ranked_matches():
    start = NOW - timedelta(hours=2)
    assert parse_summary(public_row(1, start))["avg_rank_tier"] == 54
    assert parse_summary(public_row(1, start, lobby_type=0)) is None
    assert parse_summary(public_row(1, start, duration=0)) is None
    assert parse_summary(public_row(1, start, radiant_team=[0, 0, 0, 0, 0])) is None
    # Explorer returns avg_rank_tier as a float.
    assert parse_summary(public_row(1, start, avg_rank_tier=53.6))["avg_rank_tier"] == 54


def test_store_summaries_is_idempotent(factory):
    start = NOW - timedelta(hours=2)
    rows = [public_row(100, start), public_row(101, start, radiant_win=False)]
    with factory.begin() as session:
        assert store_summaries(session, rows) == [100, 101]
    with factory.begin() as session:
        assert store_summaries(session, rows + [public_row(102, start)]) == [102]
    with factory() as session:
        assert count(session, Match) == 3
        assert count(session, MatchPlayer) == 30
        player = session.get(MatchPlayer, (101, start.replace(tzinfo=None), 7))
        assert (player.hero_id, player.is_radiant, player.win, player.gpm) == (8, False, True, None)
        assert session.get(Match, (100, start.replace(tzinfo=None))).patch_id == 60


# Aggregates


def test_draft_aggregates_are_incremental(factory):
    start = NOW - timedelta(hours=2)
    with factory.begin() as session:
        store_summaries(session, [public_row(1, start), public_row(2, start, radiant_win=False)])
    aggregates.run_aggregation(factory, batch_size=1)
    aggregates.run_aggregation(factory, batch_size=1)  # nothing pending: no double counting
    with factory.begin() as session:
        store_summaries(session, [public_row(3, start, avg_rank_tier=72)])
    result = aggregates.run_aggregation(factory, batch_size=10)
    assert result.draft_matches == 1

    with factory() as session:
        legend = session.get(PatchBracketTotal, (60, 5))
        assert (legend.games, legend.wins, legend.detailed_games) == (2, 1, 0)
        assert session.get(PatchBracketTotal, (60, 7)).games == 1
        hero_1 = session.get(HeroDraftAggregate, (60, 5, 1))
        assert (hero_1.games, hero_1.wins) == (2, 1)
        vs = session.get(HeroMatchupAggregate, (60, 5, 1, MatchupRelation.VS, 6))
        with_ = session.get(HeroMatchupAggregate, (60, 5, 1, MatchupRelation.WITH, 2))
        assert (vs.games, vs.wins, with_.games) == (2, 1, 2)
        # Per match: 10 heroes x (4 allies + 5 enemies).
        assert count(session, HeroMatchupAggregate, HeroMatchupAggregate.rank_bracket == 5) == 90


def stratz_fixture() -> dict:
    return json.loads((FIXTURES / "stratz_match.json").read_text())["data"]


def stored_stratz_match(factory) -> tuple[int, datetime]:
    """Store the STRATZ fixture's match at draft level, as publicMatches would have."""
    payload = stratz_fixture()["m9076000299"]
    heroes = {p["playerSlot"]: p["heroId"] for p in payload["players"]}
    start = datetime.fromtimestamp(payload["startDateTime"], UTC)
    row = public_row(
        9076000299,
        start,
        avg_rank_tier=53,
        duration=payload["durationSeconds"],
        radiant_win=payload["didRadiantWin"],
        radiant_team=[heroes[s] for s in range(5)],
        dire_team=[heroes[s] for s in range(128, 133)],
    )
    with factory.begin() as session:
        store_summaries(session, [row])
    return 9076000299, start


def test_apply_details_and_detail_aggregates(factory):
    match_id, start = stored_stratz_match(factory)
    with factory.begin() as session:
        assert apply_details(session, match_id, start, 60, stratz_fixture()["m9076000299"])
        assert not apply_details(session, match_id, start, 60, None)
    aggregates.run_aggregation(factory, batch_size=10)

    with factory() as session:
        mid = session.get(MatchPlayer, (match_id, start.replace(tzinfo=None), 0))
        assert (mid.hero_id, mid.position, mid.lane_role, mid.lane, mid.gpm) == (22, 2, 2, 2, 525)
        dire_safe = session.get(MatchPlayer, (match_id, start.replace(tzinfo=None), 6))
        assert (dire_safe.slot, dire_safe.position, dire_safe.lane) == (6, 1, 3)
        match = session.get(Match, (match_id, start.replace(tzinfo=None)))
        assert match.has_details and match.is_parsed and match.banned_hero_ids is None
        assert count(session, MatchEvent) == 60  # fixture keeps 6 purchases per player

        totals = session.get(PatchBracketTotal, (60, 5))
        assert (totals.games, totals.detailed_games, totals.ban_sample_games) == (1, 1, 0)
        zeus_mid = session.get(HeroAggregate, (60, 5, 2, 22))
        assert (zeus_mid.games, zeus_mid.wins, zeus_mid.sum_gpm) == (1, 1, 525)
        assert zeus_mid.sum_duration == 2535
        gpm = session.get(HeroStatHistogram, (60, 5, 2, 22, "gpm", 525))
        assert gpm.games == 1
        assert count(session, HeroStatHistogram) == 30  # 10 players x 3 stats
        # Purchases before the horn land in minute -1; repeated purchases count once.
        assert count(session, ItemTimingAggregate, ItemTimingAggregate.minute == -1) > 0
        assert session.scalar(select(func.max(ItemTimingAggregate.games))) == 1


# Patch versioning


def test_new_patch_relabels_matches_and_rebuilds(factory):
    early = datetime(2026, 10, 1, tzinfo=UTC)
    late = datetime(2026, 11, 1, tzinfo=UTC)
    with factory.begin() as session:
        store_summaries(session, [public_row(1, early), public_row(2, late, radiant_win=False)])
    aggregates.run_aggregation(factory, batch_size=10)
    with factory() as session:
        assert session.get(PatchBracketTotal, (60, 5)).games == 2

    new_patch = {"id": 61, "name": "7.42", "date": "2026-10-15T00:00:00Z"}
    with factory.begin() as session:
        assert sync_patches(session, PATCHES + [new_patch]) == [61]
        assert sync_patches(session, PATCHES + [new_patch]) == []
        assert stale_patches(session) == [60, 61]
        assert session.get(Match, (2, late.replace(tzinfo=None))).patch_id == 61
        assert session.get(MatchPlayer, (2, late.replace(tzinfo=None), 0)).patch_id == 61

    result = aggregates.run_aggregation(factory, batch_size=10)
    assert result.rebuilt_patches == [60, 61]
    with factory() as session:
        assert stale_patches(session) == []
        assert session.get(AggregateStatus, 61).rebuilt_at is not None
        assert session.get(PatchBracketTotal, (60, 5)).games == 1
        assert session.get(PatchBracketTotal, (61, 5)).games == 1

        comparison = compare_hero_patches(session, hero_id=1)
    assert comparison["before"]["patch_id"] == 60 and comparison["after"]["patch_id"] == 61
    assert comparison["before"]["win_rate"] == 1.0
    assert comparison["after"]["win_rate"] == 0.0
    assert comparison["delta"]["win_rate"] == -1.0
    assert comparison["delta"]["pick_rate"] == 0.0
    assert comparison["after"]["ban_rate"] is None


def test_hero_patch_stats_percentiles(factory):
    match_id, start = stored_stratz_match(factory)
    with factory.begin() as session:
        apply_details(session, match_id, start, 60, stratz_fixture()["m9076000299"])
    aggregates.run_aggregation(factory, batch_size=10)
    with factory() as session:
        stats = hero_patch_stats(session, 22, 60, positions=[2])
        other_position = hero_patch_stats(session, 22, 60, positions=[1])
    assert stats.detailed_games == 1 and stats.averages["gpm"] == 525
    assert 525 <= stats.percentiles["gpm"]["p50"] <= 550
    assert stats.averages["duration_minutes"] == pytest.approx(2535 / 60)
    assert other_position.detailed_games == 0 and other_position.averages["gpm"] is None
    assert stats.games == 1 and stats.pick_rate == 1.0


def test_percentile_from_histogram():
    assert percentile_from_histogram({}, 0.5, 25) is None
    assert percentile_from_histogram({400: 1, 425: 1}, 0.5, 25) == 425
    assert percentile_from_histogram({400: 2}, 0.25, 25) == 406.25


# Budget


def test_budget_reserves_and_persists(factory):
    clock = [NOW]
    limits = {OPENDOTA: (Limit("day", 3),), STRATZ: (Limit("day", 100), Limit("hour", 2))}
    budget = ApiBudget(factory, limits, clock=lambda: clock[0])
    budget.reserve(OPENDOTA, 2)
    assert ApiBudget(factory, limits, clock=lambda: clock[0]).remaining(OPENDOTA) == 1
    with pytest.raises(BudgetExceededError):
        budget.reserve(OPENDOTA, 2)
    budget.reserve(OPENDOTA)
    clock[0] = NOW + timedelta(days=1)
    assert budget.remaining(OPENDOTA) == 3

    budget.reserve(STRATZ, 2)
    with pytest.raises(BudgetExceededError) as exc:
        budget.reserve(STRATZ)
    assert exc.value.window == "hour"
    clock[0] += timedelta(hours=1)
    budget.reserve(STRATZ)


def test_budget_pace_spreads_daily_calls(factory):
    clock = [datetime(2026, 11, 4, 12, 0, tzinfo=UTC)]
    budget = ApiBudget(factory, {OPENDOTA: (Limit("day", 1440),)}, clock=lambda: clock[0])
    # 12 hours left, 10-minute runs: 72 runs share 1440 calls.
    assert budget.pace(OPENDOTA, timedelta(minutes=10)) == 20
    budget.reserve(OPENDOTA, 1440)
    assert budget.pace(OPENDOTA, timedelta(minutes=10)) == 0


# Jobs (HTTP mocked)


async def _noop_sleep(_: float) -> None:
    return None


class FakeOpenDota:
    """Routes MockTransport requests to canned responses and records them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.public_pages: dict[tuple[int, int | None], list[dict]] = {}
        self.explorer_rows: list[dict] = []
        self.explorer_sql: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path.removeprefix("/api")
        query = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        if path == "/publicMatches":
            less_than = int(query["less_than_match_id"]) if "less_than_match_id" in query else None
            return httpx.Response(
                200, json=self.public_pages.get((int(query["min_rank"]), less_than), [])
            )
        if path == "/explorer":
            sql = query["sql"]
            self.explorer_sql.append(sql)
            low = int(re.search(r"match_id > (\d+)", sql).group(1))
            high = re.search(r"match_id < (\d+)", sql)
            before = re.search(r"start_time < (\d+)", sql)
            rows = [
                r
                for r in self.explorer_rows
                if low < r["match_id"]
                and (high is None or r["match_id"] < int(high.group(1)))
                and (before is None or r["start_time"] < int(before.group(1)))
            ]
            rows.sort(key=lambda r: r["match_id"], reverse="DESC" in sql)
            limit = int(re.search(r"LIMIT (\d+)", sql).group(1))
            return httpx.Response(200, json={"rows": rows[:limit]})
        if path == "/heroStats":
            row = {"id": 1, "pro_pick": 9, "pro_win": 4, "pro_ban": 20, "turbo_picks": 7}
            row |= {f"{b}_pick": b * 10 for b in range(1, 9)} | {f"{b}_win": b for b in range(1, 9)}
            return httpx.Response(200, json=[row])
        if path == "/constants/patch":
            return httpx.Response(200, json=PATCHES)
        if path.startswith("/constants/"):
            return httpx.Response(200, json={"resource": path})
        return httpx.Response(404)


def make_context(factory, fake: FakeOpenDota, stratz_handler=None, limits=None) -> CollectorContext:
    opendota = OpenDotaClient(
        api_key=None,
        transport=httpx.MockTransport(fake.handler),
        requests_per_minute=60_000,
        sleep=_noop_sleep,
    )
    stratz = None
    if stratz_handler is not None:
        stratz = StratzClient(
            token="test-token",
            transport=httpx.MockTransport(stratz_handler),
            requests_per_minute=60_000,
            sleep=_noop_sleep,
        )
    limits = limits or {OPENDOTA: (Limit("day", 1000),), STRATZ: (Limit("day", 1000),)}
    clock = lambda: NOW  # noqa: E731
    return CollectorContext(
        factory, ApiBudget(factory, limits, clock=clock), opendota, stratz, clock
    )


@pytest.fixture
def one_bracket(monkeypatch):
    monkeypatch.setattr(jobs.settings, "collector_rank_brackets", [5])
    monkeypatch.setattr(jobs.settings, "collector_max_pages_per_run", 2)


async def test_public_matches_seeds_then_reads_settled_matches_from_explorer(factory, one_bracket):
    fake = FakeOpenDota()
    fake.public_pages[(50, None)] = [
        public_row(1000, NOW - timedelta(minutes=50)),
        public_row(990, NOW - timedelta(minutes=55)),
    ]
    ctx = make_context(factory, fake)

    first = await jobs.job_public_matches(ctx)
    assert first.rows == 2
    with factory() as session:
        cursor = session.get(CollectorCursor, "public_matches:5")
        assert cursor.newest_match_id == 989  # resume just below the seeded page
    assert (
        "match_id > 989" in fake.explorer_sql[0] and "ORDER BY match_id ASC" in fake.explorer_sql[0]
    )

    settled = NOW - timedelta(hours=3)
    fake.explorer_rows = [public_row(i, settled) for i in (995, 1001, 1002, 1003)]
    # Started 1 h ago: may still have longer games below it, so not read yet.
    fake.explorer_rows.append(public_row(1004, NOW - timedelta(hours=1)))
    second = await jobs.job_public_matches(ctx)
    assert second.rows == 4  # 995 is new too: a long game that ended after 1000 was listed
    with factory() as session:
        cursor = session.get(CollectorCursor, "public_matches:5")
        assert cursor.newest_match_id == 1003
        assert count(session, Match) == 6
        assert count(session, CollectorGap) == 0
    await ctx.opendota.aclose()


async def test_public_matches_pages_until_caught_up(factory, one_bracket, monkeypatch):
    monkeypatch.setattr(jobs.settings, "collector_explorer_rows_per_call", 2)
    monkeypatch.setattr(jobs.settings, "collector_max_pages_per_run", 10)
    fake = FakeOpenDota()
    settled = NOW - timedelta(hours=3)
    with factory.begin() as session:
        session.add(
            CollectorCursor(
                stream="public_matches:5",
                newest_match_id=100,
                newest_start_time=settled,
                updated_at=NOW - timedelta(minutes=10),
            )
        )
    fake.explorer_rows = [public_row(i, settled) for i in range(101, 106)]
    ctx = make_context(factory, fake)
    result = await jobs.job_public_matches(ctx)
    assert result.rows == 5 and len(fake.explorer_sql) == 3  # 2 + 2 + 1 (short page: done)
    with factory() as session:
        assert session.get(CollectorCursor, "public_matches:5").newest_match_id == 105
    await ctx.opendota.aclose()


async def test_lagging_stream_jumps_ahead_and_backfill_fills_the_gap(factory, one_bracket):
    fake = FakeOpenDota()
    with factory.begin() as session:
        session.add(
            CollectorCursor(
                stream="public_matches:5",
                newest_match_id=100,
                newest_start_time=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
            )
        )
    fake.public_pages[(50, None)] = [public_row(5000, NOW - timedelta(minutes=50))]
    ctx = make_context(factory, fake)
    result = await jobs.job_public_matches(ctx)
    assert result.detail.endswith("new_gaps=1")
    with factory() as session:
        gap = session.scalars(select(CollectorGap)).one()
        assert (gap.low_match_id, gap.high_match_id) == (100, 5000)
        assert session.get(CollectorCursor, "public_matches:5").newest_match_id == 4999

    old = NOW - timedelta(days=1)
    fake.explorer_rows = [public_row(i, old) for i in (100, 2500, 1500, 5000)]
    backfill = await jobs.job_backfill(ctx)
    assert backfill.rows == 2  # 100 and 5000 are the exclusive bounds
    assert "ORDER BY match_id DESC" in fake.explorer_sql[-1]
    with factory() as session:
        assert session.scalars(select(CollectorGap)).one().filled_at is not None
    assert (await jobs.job_backfill(ctx)).detail == "no open gaps"
    await ctx.opendota.aclose()


async def test_public_matches_skips_without_budget(factory, one_bracket):
    fake = FakeOpenDota()
    ctx = make_context(factory, fake, limits={OPENDOTA: (Limit("day", 0),), STRATZ: ()})
    result = await jobs.job_public_matches(ctx)
    assert result.status == "skipped" and fake.requests == []
    await ctx.opendota.aclose()


async def test_enrich_job_uses_aliased_stratz_query(factory):
    match_id, start = stored_stratz_match(factory)
    with factory.begin() as session:
        store_summaries(session, [public_row(555, start)])
    bodies: list[dict] = []

    def stratz_handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        data = stratz_fixture() | {"m555": None}
        return httpx.Response(200, json={"data": data})

    # The fixture match started 2026-11-03; enrichment wants matches between 1 h and 14 d old.
    ctx = make_context(factory, FakeOpenDota(), stratz_handler)
    result = await jobs.job_enrich(ctx)
    assert (result.rows, result.detail) == (1, "calls=1 not_found=1")
    assert "m9076000299: match(id: 9076000299)" in bodies[0]["query"]
    with factory() as session:
        assert session.get(Match, (555, start.replace(tzinfo=None))).detail_attempts == 1
        assert session.get(Match, (match_id, start.replace(tzinfo=None))).has_details
    await ctx.opendota.aclose()
    await ctx.stratz.aclose()


async def test_enrich_skips_without_token(factory):
    ctx = make_context(factory, FakeOpenDota())
    assert (await jobs.job_enrich(ctx)).status == "skipped"
    await ctx.opendota.aclose()


async def test_hero_stats_and_constants_jobs(factory):
    ctx = make_context(factory, FakeOpenDota())
    assert (await jobs.job_hero_stats(ctx)).rows == 10
    assert (await jobs.job_hero_stats(ctx)).rows == 10  # same day: replaced, not duplicated
    assert (await jobs.job_constants(ctx)).rows == len(jobs.CONSTANT_RESOURCES)
    assert (await jobs.job_constants(ctx)).rows == 0  # unchanged payloads are not stored again
    with factory() as session:
        assert count(session, HeroStatsDaily) == 10
        pro = session.get(HeroStatsDaily, (NOW.date(), "pro", 0, 1))
        assert (pro.picks, pro.wins, pro.bans) == (9, 4, 20)
        assert session.get(HeroStatsDaily, (NOW.date(), "pub", 7, 1)).picks == 70
    await ctx.opendota.aclose()


async def test_scheduler_runs_due_jobs_once(factory):
    fake = FakeOpenDota()
    ctx = make_context(factory, fake)
    specs = [jobs.JOBS["patches"], jobs.JOBS["hero-stats"], jobs.JOBS["aggregate"]]
    assert await run_due(ctx, specs) == ["patches", "hero-stats", "aggregate"]
    assert await run_due(ctx, specs) == []
    with factory() as session:
        runs = session.scalars(select(CollectorJobRun).order_by(CollectorJobRun.run_id)).all()
    assert [(r.job, r.status, r.api_calls) for r in runs] == [
        ("patches", "ok", 1),
        ("hero-stats", "ok", 1),
        ("aggregate", "ok", 0),
    ]
    await ctx.opendota.aclose()


async def test_scheduler_records_failures_and_budget_skips(factory):
    fake = FakeOpenDota()
    ctx = make_context(factory, fake, limits={OPENDOTA: (Limit("day", 0),), STRATZ: ()})

    async def boom(_ctx):
        raise ValueError("broken")

    failing = jobs.JobSpec("boom", boom, timedelta(hours=1), (), "test")
    assert await run_due(ctx, [failing, jobs.JOBS["patches"]]) == ["boom", "patches"]
    with factory() as session:
        statuses = dict(session.execute(select(CollectorJobRun.job, CollectorJobRun.status)).all())
    assert statuses == {"boom": "failed", "patches": "skipped"}
    await ctx.opendota.aclose()


def test_cli_lists_jobs(capsys):
    from dota_insights.collector.cli import main

    assert main(["list"]) == 0
    out = capsys.readouterr().out
    for name in jobs.JOBS:
        assert name in out


async def test_enrich_retries_unparsed_matches_later(factory):
    start = NOW - timedelta(hours=2)
    with factory.begin() as session:
        store_summaries(session, [public_row(1, start), public_row(2, start)])
        session.get(Match, (2, start)).detail_attempts = 1  # retried only once 3 h old
    asked: list[str] = []

    def stratz_handler(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"data": {"m1": None}})

    ctx = make_context(factory, FakeOpenDota(), stratz_handler)
    await jobs.job_enrich(ctx)
    assert "m1: match(id: 1)" in asked[0] and "m2:" not in asked[0]
    with factory() as session:
        assert session.get(Match, (1, start)).detail_attempts == 1
    await ctx.opendota.aclose()
    await ctx.stratz.aclose()
