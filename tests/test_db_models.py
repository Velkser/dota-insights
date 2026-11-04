import io
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from dota_insights.db.models import PARTITIONED_TABLES, Base
from dota_insights.db.session import database_url

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "patches",
    "matches",
    "match_players",
    "player_timeseries",
    "match_events",
    "patch_bracket_totals",
    "hero_aggregates",
    "hero_matchup_aggregates",
    "item_timing_aggregates",
    "hero_draft_aggregates",
    "hero_stat_histograms",
    "aggregate_status",
    "hero_stats_daily",
    "reference_snapshots",
    "collector_job_runs",
    "collector_api_usage",
    "collector_cursors",
    "collector_gaps",
}

EXPECTED_INDEXES = {
    "matches": {
        "ix_matches_patch_rank",
        "ix_matches_pending_aggregation",
        "ix_matches_pending_details_aggregation",
    },
    "match_players": {
        "ix_match_players_patch_hero_position",
        "ix_match_players_account_start_time",
    },
    "match_events": {"ix_match_events_type_item_time"},
    "hero_aggregates": {"ix_hero_aggregates_hero_patch"},
    "item_timing_aggregates": {"ix_item_timing_aggregates_item_patch"},
}


def alembic_config(output: io.StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_metadata_has_expected_tables():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


@pytest.mark.parametrize("table", sorted(EXPECTED_INDEXES))
def test_expected_indexes_present(table):
    names = {index.name for index in Base.metadata.tables[table].indexes}
    assert EXPECTED_INDEXES[table] <= names


def test_metadata_compiles_for_postgresql():
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        CreateTable(table).compile(dialect=dialect)


@pytest.mark.parametrize("name", PARTITIONED_TABLES)
def test_per_match_tables_partitioned_by_start_time(name):
    table = Base.metadata.tables[name]
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (start_time)"
    # PostgreSQL requires the partition key in every unique constraint, the primary key included.
    assert "start_time" in table.primary_key.columns


def test_database_url_selects_psycopg_driver():
    url = database_url("postgresql://user@localhost:5432/dota_insights")
    assert url.drivername == "postgresql+psycopg"
    assert url.database == "dota_insights"


def test_database_url_requires_setting(monkeypatch):
    monkeypatch.setattr("dota_insights.db.session.settings.database_url", None)
    with pytest.raises(RuntimeError):
        database_url()


def test_offline_migration_renders():
    output = io.StringIO()
    command.upgrade(alembic_config(output), "head", sql=True)
    sql = output.getvalue()
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table} " in sql
    assert sql.count("PARTITION BY RANGE (start_time)") == len(PARTITIONED_TABLES)
    assert "CREATE OR REPLACE FUNCTION ensure_month_partitions" in sql


@pytest.mark.skipif(
    "DOTA_INSIGHTS_TEST_DATABASE_URL" not in os.environ,
    reason="set DOTA_INSIGHTS_TEST_DATABASE_URL to an empty scratch database",
)
def test_online_migration_round_trip():
    url = os.environ["DOTA_INSIGHTS_TEST_DATABASE_URL"]
    config = alembic_config()
    config.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()
    command.upgrade(config, "head")
    try:
        engine = create_engine(database_url(url))
        with engine.begin() as conn:
            conn.execute(text("SET TIME ZONE 'America/Los_Angeles'"))
            conn.execute(text("SELECT ensure_month_partitions('2030-03-15 12:00:00+00')"))
            bound = conn.execute(
                text(
                    "SELECT pg_get_expr(relpartbound, oid) FROM pg_class "
                    "WHERE relname = 'matches_2030_03'"
                )
            ).scalar_one()
        assert "2030-02-28 16:00:00-08" in bound and "2030-03-31 17:00:00-07" in bound
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
        engine.dispose()
    finally:
        command.downgrade(config, "base")


@pytest.mark.skipif(
    "DOTA_INSIGHTS_TEST_DATABASE_URL" not in os.environ,
    reason="set DOTA_INSIGHTS_TEST_DATABASE_URL to an empty scratch database",
)
def test_collector_round_trip_on_postgresql():
    """Collector writes on real partitions, arrays and RETURNING, then a patch-split rebuild."""
    from datetime import UTC, datetime

    from sqlalchemy.orm import sessionmaker

    from dota_insights.collector import aggregates
    from dota_insights.collector.ingest import store_summaries
    from dota_insights.collector.patches import stale_patches, sync_patches
    from dota_insights.db.models import Match, PatchBracketTotal

    url = os.environ["DOTA_INSIGHTS_TEST_DATABASE_URL"]
    config = alembic_config()
    config.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()
    command.upgrade(config, "head")
    engine = create_engine(database_url(url))
    try:
        factory = sessionmaker(engine, expire_on_commit=False)
        patches = [{"id": 60, "name": "7.41", "date": "2026-03-24T00:50:59Z"}]
        # 2030-05 is outside the pre-created partitions: the collector must create it.
        starts = [datetime(2026, 10, 1, tzinfo=UTC), datetime(2030, 5, 2, tzinfo=UTC)]
        rows = [
            {
                "match_id": 10 + i,
                "start_time": int(start.timestamp()),
                "duration": 2000,
                "lobby_type": 7,
                "game_mode": 22,
                "avg_rank_tier": 61.4,
                "radiant_win": True,
                "radiant_team": [1, 2, 3, 4, 5],
                "dire_team": [6, 7, 8, 9, 10],
            }
            for i, start in enumerate(starts)
        ]
        with factory.begin() as session:
            sync_patches(session, patches)
            assert store_summaries(session, rows) == [10, 11]
            assert store_summaries(session, rows) == []
        aggregates.run_aggregation(factory, batch_size=10)
        with factory.begin() as session:
            assert session.get(PatchBracketTotal, (60, 6)).games == 2
            sync_patches(
                session, patches + [{"id": 61, "name": "7.42", "date": "2027-01-01T00:00:00Z"}]
            )
            assert stale_patches(session) == [60, 61]
        assert aggregates.run_aggregation(factory, batch_size=10).rebuilt_patches == [60, 61]
        with factory() as session:
            assert session.get(PatchBracketTotal, (60, 6)).games == 1
            assert session.get(PatchBracketTotal, (61, 6)).games == 1
            assert session.get(Match, (11, starts[1])).patch_id == 61
    finally:
        engine.dispose()
        command.downgrade(config, "base")


def test_database_url_keeps_neon_ssl_parameters():
    url = database_url(
        "postgres://user:pw@ep-x-123.eu-central-1.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    assert url.drivername == "postgresql+psycopg"
    assert dict(url.query) == {"sslmode": "require", "channel_binding": "require"}
