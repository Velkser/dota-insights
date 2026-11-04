"""Tools reading the collector's aggregates first, on a seeded SQLite database.

The database is filled the way production fills it: synthetic ranked matches (with details and
purchases) go through the collector's own aggregation. Every test records the HTTP requests the
tools make, so a database hit is checked to cost no API call, and the fallbacks (stale or old
aggregates, small samples, unsupported filters, a broken database, no database) are checked to
go back to the live sources.
"""

import json
import random
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from sqlalchemy import create_engine, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from dota_insights import sources
from dota_insights.analytics import matchups
from dota_insights.cache import clear_all
from dota_insights.collector.aggregates import run_aggregation
from dota_insights.collector.patches import sync_patches
from dota_insights.db import readers
from dota_insights.db.access import read, use_database
from dota_insights.db.models import (
    AggregateStatus,
    Base,
    EventType,
    HeroStatsDaily,
    Match,
    MatchEvent,
    MatchPlayer,
    MatchSource,
)
from dota_insights.mcp.envelope import DB_SOURCE
from dota_insights.mcp.server import mcp
from dota_insights.models import Filters, GameMode
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

OPENDOTA_HERO_STATS = json.loads(
    (Path(__file__).parent / "fixtures" / "heroes" / "opendota_hero_stats.json").read_text()
)
PATCHES = [
    {"id": 59, "name": "7.40", "date": "2025-12-16T00:50:40.281Z"},
    {"id": 60, "name": "7.41", "date": "2026-03-24T00:50:59.580Z"},
]
HEROES = [1, 2, 8, 11, 14, 17, 22, 26, 35, 44, 74, 86]
BKB, TREADS = 116, 63
CURRENT_MATCHES = 400
OLD_MATCHES = 120


def _matches(rng: random.Random, count: int, first_id: int, newest: datetime, patch_id: int):
    for i in range(count):
        # SQLite stores timestamps naive (in UTC); aware keys confuse its bulk insert.
        start = (newest - timedelta(minutes=2 * i)).replace(tzinfo=None)
        picks = rng.sample(HEROES, 10)
        radiant_edge = 0.1 * ((1 in picks[:5]) - (1 in picks[5:]))
        radiant_win = rng.random() < 0.5 + radiant_edge
        match_id = first_id + i
        match = Match(
            match_id=match_id,
            start_time=start,
            duration=rng.randint(1800, 3000),
            patch_id=patch_id,
            game_mode=22,
            lobby_type=7,
            avg_rank_tier=55 if i % 2 else 72,
            radiant_win=radiant_win,
            source=MatchSource.OPENDOTA,
            has_details=True,
        )
        players, events = [], []
        for slot, hero in enumerate(picks):
            position = slot % 5 + 1
            is_radiant = slot < 5
            players.append(
                MatchPlayer(
                    match_id=match_id,
                    start_time=start,
                    slot=slot,
                    patch_id=patch_id,
                    hero_id=hero,
                    is_radiant=is_radiant,
                    position=position,
                    kills=rng.randint(0, 12),
                    deaths=rng.randint(0, 10),
                    assists=rng.randint(0, 20),
                    gpm=350 + 60 * (5 - position) + rng.randint(0, 100),
                    xpm=400 + 50 * (5 - position) + rng.randint(0, 100),
                    last_hits=rng.randint(20, 300),
                    net_worth=rng.randint(8000, 30000),
                    hero_damage=rng.randint(5000, 40000),
                    tower_damage=rng.randint(0, 8000),
                    healing=0,
                    win=is_radiant == radiant_win,
                )
            )
            bought = [(TREADS, rng.randint(6, 14) * 60)]
            if position <= 3:
                bought.append((BKB, rng.randint(14, 30) * 60 + 30))
            for item_id, time in bought:
                events.append(
                    MatchEvent(
                        match_id=match_id,
                        start_time=start,
                        seq=len(events),
                        event_type=EventType.PURCHASE,
                        time=time,
                        slot=slot,
                        item_id=item_id,
                        payload={},
                    )
                )
        yield match, players, events


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Path:
    """A database file seeded and aggregated once; each test gets its own copy."""
    path = tmp_path_factory.mktemp("db") / "seed.db"
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    rng = random.Random(7)
    newest = datetime.now(UTC) - timedelta(hours=3)
    with factory.begin() as session:
        sync_patches(session, PATCHES)
        for patch_id, count, first_id, start in (
            (60, CURRENT_MATCHES, 9_000_000, newest),
            (59, OLD_MATCHES, 8_000_000, datetime(2026, 3, 1, tzinfo=UTC)),
        ):
            for match, players, events in _matches(rng, count, first_id, start, patch_id):
                session.add(match)
                session.add_all(players)
                session.add_all(events)
        today = datetime.now(UTC).date()
        session.add_all(
            HeroStatsDaily(
                day=today, scope="pro", rank_bracket=0, hero_id=h, picks=40, wins=22, bans=10
            )
            for h in HEROES
        )
    run_aggregation(factory, batch_size=1000)
    engine.dispose()
    return path


@pytest.fixture
def engine(seeded, tmp_path):
    path = tmp_path / "aggregates.db"
    shutil.copyfile(seeded, path)
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    yield engine
    engine.dispose()


class Requests:
    """Records every API request; STRATZ refuses and OpenDota only serves /heroStats."""

    def __init__(self) -> None:
        self.made: list[str] = []

    def stratz(self, request: httpx.Request) -> httpx.Response:
        self.made.append("stratz")
        return httpx.Response(401)  # not retried, so fallbacks stay fast

    def opendota(self, request: httpx.Request) -> httpx.Response:
        self.made.append(f"opendota {request.url.path}")
        if request.url.path.endswith("/heroStats"):
            return httpx.Response(200, json=OPENDOTA_HERO_STATS)
        return httpx.Response(404)


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def requests(engine, monkeypatch):
    # The seeded database is small; let its position shares stand in for STRATZ's.
    monkeypatch.setattr(matchups, "MIN_DB_POSITION_MATCHES", 100)
    clear_all()
    recorder = Requests()
    sources.use_clients(
        stratz=StratzClient(
            token="t", transport=httpx.MockTransport(recorder.stratz), sleep=_no_sleep
        ),
        opendota=OpenDotaClient(transport=httpx.MockTransport(recorder.opendota), sleep=_no_sleep),
    )
    with use_database(engine):
        yield recorder
    await sources.close_clients()
    clear_all()


async def _call(name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args, raise_on_error=False)
    return result.structured_content


DRAFT = {
    "radiant": ["am", "axe", "jugg", "sf", "pudge"],
    "dire": ["storm", "zeus", "lion", "sniper", "pa"],
}
SMALL = {"min_sample": 50}
DB_HITS = [
    ("get_tier_list", {}),
    ("find_hidden_gems", {"max_pick_rate": 1.0}),
    ("get_hero_overview", {"hero": "am"}),
    ("compare_heroes", {"heroes": ["am", "axe"]}),
    ("compare_heroes", {"heroes": ["am", "axe"], "filters": {"position": 1, "min_sample": 30}}),
    ("get_hero_matchups", {"hero": "am", "other_position": 5, "filters": SMALL}),
    ("find_hero_combos", {"positions": [1, 5], "filters": SMALL}),
    ("suggest_pick", {"allies": ["am"], "enemies": ["axe"], "position": 2, "filters": SMALL}),
    ("analyze_draft", {**DRAFT, "filters": SMALL}),
    ("get_item_overview", {"item": "bkb"}),
    ("analyze_item", {"item": "bkb", "hero": "am"}),
    ("analyze_item", {"item": "bkb", "filters": {"rank_bracket": "divine"}}),
    ("compare_items", {"items": ["bkb", "power treads"], "hero": "am"}),
    ("get_hero_benchmarks", {"hero": "am", "value": {"gpm": 520}}),
    ("get_hero_benchmarks", {"hero": "am", "filters": {"rank_bracket": "legend+"}}),
]


@pytest.mark.parametrize(
    ("tool", "args"), DB_HITS, ids=[f"{t}-{i}" for i, (t, _) in enumerate(DB_HITS)]
)
async def test_database_hit_makes_no_api_call(requests, tool, args):
    body = await _call(tool, args)
    assert body["status"] == "ok", body.get("message")
    assert requests.made == []
    assert body["source"][0] == DB_SOURCE
    assert "opendota" in body["source"]
    assert body["period"]["to_date"] == str((datetime.now(UTC) - timedelta(hours=3)).date())
    assert body["data_freshness"] is not None
    assert any("collector's database" in c for c in body["caveats"])


async def test_tier_list_from_database(requests):
    body = await _call("get_tier_list", {"limit": 130})
    result = body["result"]
    assert body["sample_size"] == CURRENT_MATCHES
    assert body["filters"]["patch"] == "7.41"
    assert body["filters"]["game_mode"] == "ranked_all_pick"
    assert body["source"] == [DB_SOURCE, "opendota"]
    assert {h["hero_id"] for h in result["heroes"]} == set(HEROES)
    # Anti-Mage's team wins 60% of its games in the seed, so it tops the list.
    assert result["heroes"][0]["hero_id"] == 1
    assert sum(h["matches"] for h in result["heroes"]) == CURRENT_MATCHES * 10
    assert any("Immortal" in c for c in body["caveats"])


async def test_hero_overview_from_database(requests):
    body = await _call("get_hero_overview", {"hero": "am"})
    overview = body["result"]
    assert body["source"] == [DB_SOURCE, "opendota", "stratz"]
    assert [row["group"] for row in overview["by_bracket"]] == [
        "herald", "guardian", "crusader", "archon", "legend", "ancient", "divine+immortal",
    ]  # fmt: skip
    by_bracket = {row["group"]: row["matches"] for row in overview["by_bracket"]}
    assert by_bracket["legend"] + by_bracket["divine+immortal"] == overview["current"]["matches"]
    shares = [row["share_of_hero_games"] for row in overview["by_position"]]
    assert sum(shares) == pytest.approx(1.0)
    assert [t["patch"] for t in overview["trend"]] == ["7.40", "7.41"]
    # Collection of 7.40 started weeks after its release, 7.41 is live: both partial.
    assert all(t["partial"] for t in overview["trend"])
    assert overview["trend"][0]["matches"] > 0
    assert overview["pro"]["picks"] == 40 and overview["pro"]["pro_matches"] == 48


async def test_overview_keeps_database_pool_when_breakdowns_fail(requests, monkeypatch):
    from dota_insights.analytics import heroes

    def broken(session, scope, hero_id):
        raise SQLAlchemyError("gone")

    monkeypatch.setattr(heroes, "_db_overview", broken)
    body = await _call("get_hero_overview", {"hero": "am"})
    assert body["status"] == "ok"
    assert body["source"][0] == DB_SOURCE
    assert body["result"]["by_bracket"] == [] and body["result"]["pro"] is not None
    assert requests.made == ["opendota /api/heroStats"]  # pro rates only


async def test_position_pool_uses_enriched_matches(requests):
    body = await _call("get_tier_list", {"filters": {"position": 1, "min_sample": 30}})
    assert body["status"] == "ok"
    assert body["sample_size"] == CURRENT_MATCHES  # enriched matches
    assert body["source"] == [DB_SOURCE, "opendota", "stratz"]
    assert sum(h["matches"] for h in body["result"]["heroes"]) == CURRENT_MATCHES * 2


async def test_rank_filter_selects_brackets(requests):
    body = await _call("get_tier_list", {"filters": {"rank_bracket": "legend"}})
    assert body["sample_size"] == CURRENT_MATCHES // 2
    assert not any("Immortal" in c for c in body["caveats"])
    body = await _call("get_tier_list", {"filters": {"rank_bracket": "divine+"}})
    assert body["sample_size"] == CURRENT_MATCHES // 2


async def test_older_patch_from_database(requests):
    body = await _call("get_tier_list", {"filters": {"patch": "7.40"}})
    assert body["status"] == "ok"
    assert body["sample_size"] == OLD_MATCHES
    assert body["period"] == {"from_date": "2026-02-28", "to_date": "2026-03-01"}
    assert requests.made == []


async def test_matchups_from_database(requests):
    body = await _call("get_hero_matchups", {"hero": "am", "filters": SMALL})
    result = body["result"]
    assert body["source"] == [DB_SOURCE, "opendota"]
    pairs = result["counters"] + result["easiest_matchups"]
    assert pairs and all(p["games"] >= 50 for p in pairs)
    assert result["games"] > 0


async def test_item_overview_counts_first_purchases(requests):
    body = await _call("get_item_overview", {"item": "bkb"})
    result = body["result"]
    assert result["buyers"] == CURRENT_MATCHES * 6  # positions 1-3 on both teams
    assert result["player_games"] == CURRENT_MATCHES * 10
    assert 14 <= result["median_purchase_minute"] <= 30
    assert body["source"] == [DB_SOURCE, "opendota", "stratz"]
    assert body["filters"] is not None


async def test_benchmarks_from_histograms(requests):
    body = await _call("get_hero_benchmarks", {"hero": "am", "value": {"gpm": 520, "xpm": 500}})
    result = body["result"]
    assert set(result["metrics"]) == {"gold_per_min", "xp_per_min"}
    assert [p["percentile"] for p in result["metrics"]["gold_per_min"]][-1] == 0.99
    assert {p["metric"] for p in result["placements"]} == {"gold_per_min", "xp_per_min"}
    assert body["sample_size"] > 100


# Fallbacks


async def _tier_list_falls_back(requests) -> dict:
    body = await _call("get_tier_list", {})
    assert DB_SOURCE not in body["source"]
    assert "stratz" in requests.made
    return body


async def test_stale_patch_falls_back(requests, engine):
    with engine.begin() as conn:
        conn.execute(update(AggregateStatus).values(stale=True))
    body = await _tier_list_falls_back(requests)
    assert body["source"] == ["opendota"]


async def test_old_aggregates_fall_back(requests, engine):
    old = datetime.now(UTC) - readers.MAX_AGE - timedelta(hours=1)
    with engine.begin() as conn:
        conn.execute(
            update(AggregateStatus).where(AggregateStatus.patch_id == 60).values(last_match_at=old)
        )
    await _tier_list_falls_back(requests)


async def test_small_sample_falls_back(requests):
    body = await _call("get_tier_list", {"filters": {"min_sample": CURRENT_MATCHES + 1}})
    assert DB_SOURCE not in body["source"]
    assert "stratz" in requests.made


async def test_hero_below_min_sample_falls_back(requests):
    # Lina (25) was never picked in the seed.
    await _call("get_hero_overview", {"hero": "lina"})
    assert "stratz" in requests.made


async def test_item_below_min_sample_falls_back(requests):
    body = await _call("get_item_overview", {"item": "battle fury"})
    assert "stratz" in requests.made
    assert DB_SOURCE not in body["source"]


@pytest.mark.parametrize(
    "filters",
    [{"rank_bracket": "immortal"}, {"game_mode": "turbo"}, {"patch": "7.41c"}],
)
async def test_unsupported_filters_fall_back(requests, filters):
    await _call("get_tier_list", {"filters": filters})
    assert "stratz" in requests.made


async def test_benchmarks_for_other_metrics_use_opendota(requests):
    await _call("get_hero_benchmarks", {"hero": "am", "value": {"last_hits_per_min": 7}})
    assert set(requests.made) == {"opendota /api/benchmarks"}


async def test_no_database_uses_live_sources(requests):
    with use_database(None):
        await _tier_list_falls_back(requests)


async def test_broken_database_falls_back(requests, tmp_path):
    empty = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", poolclass=NullPool)
    with use_database(empty):
        await _tier_list_falls_back(requests)
    empty.dispose()


# Readers and collector bookkeeping


async def test_scope_for_maps_filters(engine):
    with use_database(engine):
        scope = await read(readers.scope_for, Filters(rank_bracket="ancient+", position=3))
        assert isinstance(scope, readers.Scope)
        assert (scope.patch, scope.brackets, scope.position, scope.current) == (
            "7.41",
            (6, 7),
            3,
            True,
        )
        for filters in (
            Filters(rank_bracket="immortal"),
            Filters(game_mode=GameMode.TURBO),
            Filters(patch="7.41c"),
            Filters(patch="7.39"),
        ):
            assert isinstance(await read(readers.scope_for, filters), str)


def test_aggregation_records_period(engine):
    with sessionmaker(engine)() as session:
        status = session.get(AggregateStatus, 60)
        assert status.first_match_at < status.last_match_at
        span = status.last_match_at - status.first_match_at
        assert span == timedelta(minutes=2 * (CURRENT_MATCHES - 1))


def test_rebuild_resets_period(engine):
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        before = session.get(AggregateStatus, 60).first_match_at
        session.execute(
            update(AggregateStatus)
            .where(AggregateStatus.patch_id == 60)
            .values(stale=True, first_match_at=datetime(2020, 1, 1))
        )
    run_aggregation(factory, batch_size=1000)
    with factory() as session:
        status = session.get(AggregateStatus, 60)
        assert not status.stale
        assert status.first_match_at == before
