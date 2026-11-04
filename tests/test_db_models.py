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
}

EXPECTED_INDEXES = {
    "matches": {"ix_matches_patch_rank"},
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
