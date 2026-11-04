"""Developer API (p5-06): /api/v1 routes, API keys, quotas, usage channel and the OpenAPI doc.

Requests go through the real ASGI app; keys live in SQLite, the key cache and quota counters
in a fake Redis, and the one tool that needs data has its analytics replaced.
"""

import json

import httpx
import pytest
from fakes import FakeKV
from openapi_spec_validator import validate
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dota_insights.analytics import heroes as hero_analytics
from dota_insights.api import cli, keys
from dota_insights.api.context import ApiCaller
from dota_insights.api.openapi import build_document
from dota_insights.api.routes import ENDPOINTS, PREFIX
from dota_insights.collector.db import utc_now
from dota_insights.config import settings
from dota_insights.db.access import use_database
from dota_insights.db.models import ApiKey, Base, UsageEvent
from dota_insights.kv import reset_kv, use_kv
from dota_insights.mcp.asgi import app
from dota_insights.mcp.ratelimit import limits_for
from dota_insights.mcp.server import mcp
from dota_insights.models import Filters
from dota_insights.usage import store
from dota_insights.usage.report import build_report


@pytest.fixture
def kv():
    fake = FakeKV()
    use_kv(fake)
    keys.clear_cache()
    yield fake
    reset_kv()
    keys.clear_cache()


@pytest.fixture
def engine(tmp_path, monkeypatch, kv):
    monkeypatch.setattr(settings, "api_key_secret", "test-secret")
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    with use_database(engine):
        yield engine
    engine.dispose()


def new_key(engine, **quotas) -> tuple[str, str]:
    with Session(engine) as session, session.begin():
        row, key = keys.create_key(session, "test client", **quotas)
        return row.id, key


@pytest.fixture
def tier_list(monkeypatch):
    """``get_tier_list`` without sources: one hero, 1,000 matches."""
    seen: list[Filters] = []

    async def fake(filters: Filters, limit: int):
        seen.append(filters)
        entry = hero_analytics.TierEntry(
            rank=1,
            tier="S",
            hero="Anti-Mage",
            hero_id=1,
            matches=500,
            win_rate=0.55,
            win_rate_ci=(0.51, 0.59),
            smoothed_win_rate=0.54,
            pick_rate=0.1,
        )
        pool = hero_analytics.Pool(
            counts={},
            total_matches=1000,
            filters=filters,
            source="stratz",
            patch="7.41f",
            period=None,
            freshness=None,
        )
        return hero_analytics.Analysis(
            result=hero_analytics.TierList(
                pool_win_rate=0.5, heroes=[entry], excluded_low_sample=0
            ),
            pool=pool,
            sample_size=1000,
            sources=["stratz"],
            caveats=[],
        )

    monkeypatch.setattr(hero_analytics, "tier_list", fake)
    return seen


async def get(path: str, key: str | None = None, **params) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        return await client.get(path, headers=headers, params=params)


def queued(kv: FakeKV) -> list[dict]:
    return [json.loads(item) for item in kv.lists.get(store.QUEUE_KEY, [])]


# Routes


async def test_endpoint_returns_the_tools_envelope_with_filters_from_the_query(engine, tier_list):
    _, key = new_key(engine)
    response = await get(
        f"{PREFIX}/tier-list", key, limit="5", patch="7.41", position="1", rank_bracket="divine+"
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "ok"
    assert body["sample_size"] == 1000
    assert body["result"]["heroes"][0]["hero"] == "Anti-Mage"
    assert body["filters"]["patch"] == "7.41" and body["filters"]["position"] == 1
    assert body["caveats"] and body["source"] == ["stratz"]
    assert tier_list[0].rank_bracket == "divine+"


async def test_same_envelope_as_the_mcp_tool(engine, tier_list):
    _, key = new_key(engine)
    response = await get(f"{PREFIX}/tier-list", key)
    tool = await mcp.call_tool("get_tier_list", {})
    assert response.json() == tool.structured_content


async def test_tool_level_errors_are_envelopes_with_status_200(engine):
    _, key = new_key(engine)
    response = await get(f"{PREFIX}/heroes/zzzzzzzz", key)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error" and "No hero matches" in body["message"]


async def test_invalid_and_unknown_parameters_answer_422(engine):
    _, key = new_key(engine)
    bad = await get(f"{PREFIX}/matches/abc", key)
    assert bad.status_code == 422
    assert bad.json()["status"] == "error" and "match_id" in bad.json()["message"]

    bad_filter = await get(f"{PREFIX}/tier-list", key, position="seven")
    assert bad_filter.status_code == 422 and "position" in bad_filter.json()["message"]

    unknown = await get(f"{PREFIX}/tier-list", key, colour="red")
    assert unknown.status_code == 422
    assert "colour" in unknown.json()["message"] and "patch" in unknown.json()["hint"]


async def test_unknown_path_is_a_json_404_and_index_lists_endpoints(engine):
    _, key = new_key(engine)
    missing = await get(f"{PREFIX}/nothing/here", key)
    assert missing.status_code == 404 and missing.json()["status"] == "error"

    index = (await get(PREFIX)).json()
    assert {e["tool"] for e in index["endpoints"]} == {e.tool for e in ENDPOINTS}


# Authentication


async def test_missing_key_answers_401_envelope(engine):
    response = await get(f"{PREFIX}/tier-list")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    body = response.json()
    assert body["status"] == "error" and body["message"] == "Missing API key."
    assert body["hint"] and body["result"] is None


async def test_wrong_key_and_wrong_scheme_answer_401(engine):
    _, key = new_key(engine)
    assert (await get(f"{PREFIX}/tier-list", "di_not-a-real-key")).status_code == 401
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        basic = await client.get(f"{PREFIX}/tier-list", headers={"Authorization": f"Basic {key}"})
    assert basic.status_code == 401


async def test_revoked_key_is_refused(engine, tier_list, kv):
    key_id, key = new_key(engine)
    assert (await get(f"{PREFIX}/tier-list", key)).status_code == 200
    with Session(engine) as session, session.begin():
        row = keys.revoke_key(session, key_id)
        record, key_hash = keys.record_of(row), row.key_hash
    await keys.publish(key_hash, record)
    response = await get(f"{PREFIX}/tier-list", key)
    assert response.status_code == 401
    assert response.json()["message"] == "Invalid or revoked API key."


async def test_revocation_reaches_other_instances_through_redis(engine, kv):
    key_id, key = new_key(engine)
    assert (await keys.authenticate(key)).valid  # now cached in Redis
    with Session(engine) as session, session.begin():
        keys.revoke_key(session, key_id)
    # Another instance: empty local cache, the Redis entry still says active.
    keys.clear_cache()
    assert (await keys.authenticate(key)).valid
    await keys.publish(keys.hash_key(key), keys.KeyRecord(id=key_id, revoked=True))
    keys.clear_cache()
    assert not (await keys.authenticate(key)).valid


async def test_without_secret_or_database_the_api_answers_503(engine, monkeypatch):
    _, key = new_key(engine)
    with use_database(None):
        keys.clear_cache()
        down = await get(f"{PREFIX}/tier-list", key)
        assert down.status_code == 503 and down.json()["status"] == "error"
    monkeypatch.setattr(settings, "api_key_secret", None)
    off = await get(f"{PREFIX}/tier-list", key)
    assert off.status_code == 503 and "not enabled" in off.json()["message"]


def test_only_the_hash_is_stored(engine):
    key_id, key = new_key(engine)
    with Session(engine) as session:
        row = session.get(ApiKey, key_id)
        stored = [str(v) for v in (row.key_hash, row.prefix, row.name, row.id)]
    assert key.startswith("di_") and len(key) > 40
    assert key not in stored
    assert row.key_hash == keys.hash_key(key) and len(row.key_hash) == 64
    assert key.startswith(row.prefix)


# Quotas


async def test_per_minute_quota_answers_429_with_retry_after(engine, tier_list, kv):
    key_id, key = new_key(engine, per_minute=2, per_day=100)
    for _ in range(2):
        assert (await get(f"{PREFIX}/tier-list", key)).status_code == 200
    response = await get(f"{PREFIX}/tier-list", key)
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 60
    body = response.json()
    assert body["status"] == "error" and "your API key" in body["message"]
    assert any(k.startswith(f"di:rl:key-{key_id}:all:60:") for k in kv.data)
    # Another key has its own quota.
    _, other = new_key(engine, per_minute=2)
    assert (await get(f"{PREFIX}/tier-list", other)).status_code == 200


async def test_per_day_quota(engine, tier_list):
    _, key = new_key(engine, per_minute=100, per_day=1)
    assert (await get(f"{PREFIX}/tier-list", key)).status_code == 200
    response = await get(f"{PREFIX}/tier-list", key)
    assert response.status_code == 429 and "a day" in response.json()["message"]


async def test_key_quotas_hold_even_with_ip_limits_off(engine, tier_list):
    assert settings.rate_limit_enabled is False  # tests/conftest.py
    _, key = new_key(engine, per_minute=1)
    await get(f"{PREFIX}/tier-list", key)
    assert (await get(f"{PREFIX}/tier-list", key)).status_code == 429


def test_key_limits_replace_ip_limits_and_keep_heavy_ones():
    caller = ApiCaller(key_id="k", per_minute=7, per_day=70)
    limits = {(lim.scope, lim.window): lim.max_calls for lim in limits_for("get_tier_list", caller)}
    assert limits[("all", 60)] == 7 and limits[("all", 86400)] == 70
    assert limits[("heavy", 60)] == settings.rate_limit_heavy_per_minute


async def test_default_quotas_come_from_settings(engine, monkeypatch, tier_list):
    monkeypatch.setattr(settings, "api_key_per_minute", 1)
    _, key = new_key(engine)
    await get(f"{PREFIX}/tier-list", key)
    assert (await get(f"{PREFIX}/tier-list", key)).status_code == 429


# Usage analytics


async def test_api_calls_are_recorded_with_the_api_channel(engine, tier_list, kv):
    _, key = new_key(engine, per_minute=1)
    await get(f"{PREFIX}/tier-list", key, patch="7.41")
    await get(f"{PREFIX}/tier-list", key)  # refused by the quota, still recorded
    await mcp.call_tool("get_tier_list", {})
    events = queued(kv)
    assert [(e["tool"], e["channel"], e["status"]) for e in events] == [
        ("get_tier_list", "api", "ok"),
        ("get_tier_list", "api", "error"),
        ("get_tier_list", "mcp", "ok"),
    ]
    assert events[0]["arguments"] == {"filters": {"patch": "7.41"}}
    assert events[1]["error_kind"] == "rate_limited"
    assert key not in json.dumps(events)

    with Session(engine) as session, session.begin():
        assert store.store_events(session, [*events, {**events[0], "event_id": "x" * 32}]) == 4
    with Session(engine) as session:
        channels = list(session.scalars(select(UsageEvent.channel)))
        report = build_report(session, utc_now(), 1)
    assert sorted(channels) == ["api", "api", "api", "mcp"]
    assert report["channels"] == {"api": 3, "mcp": 1}


def test_events_queued_before_the_channel_existed_are_mcp(engine):
    event = {"type": "call", "event_id": "e" * 32, "ts": "2026-11-04T12:00:00+00:00"}
    with Session(engine) as session, session.begin():
        store.store_events(session, [{**event, "tool": "server_info", "status": "ok"}])
    with Session(engine) as session:
        assert session.scalar(select(UsageEvent.channel)) == "mcp"


# OpenAPI


async def test_openapi_document_is_valid_and_covers_every_endpoint():
    response = await get(f"{PREFIX}/openapi.json")
    assert response.status_code == 200
    document = response.json()
    validate(document)
    assert document["openapi"].startswith("3.1")
    operations = {path: item["get"] for path, item in document["paths"].items()}
    assert set(operations) == {PREFIX + e.path for e in ENDPOINTS}
    assert {op["operationId"] for op in operations.values()} == {e.tool for e in ENDPOINTS}
    for op in operations.values():
        assert {"200", "401", "422", "429"} <= set(op["responses"])
    params = {p["name"]: p for p in operations[f"{PREFIX}/heroes/{{hero}}/matchups"]["parameters"]}
    assert params["hero"]["in"] == "path" and params["hero"]["required"]
    assert {"mode", "limit", "patch", "rank_bracket", "position", "min_sample"} <= set(params)
    assert "$defs" not in json.dumps(document)
    envelope = document["components"]["schemas"]["GetTierListEnvelope"]
    assert {"status", "result", "sample_size", "caveats"} <= set(envelope["properties"])


async def test_openapi_parameters_are_the_ones_the_routes_accept(engine, tier_list):
    """Every documented query parameter is accepted (not answered with 422 'unknown')."""
    document = await build_document(mcp)
    query = [p["name"] for p in document["paths"][f"{PREFIX}/tier-list"]["get"]["parameters"]]
    _, key = new_key(engine, per_minute=100)
    values = {"limit": "5", "patch": "7.41", "rank_bracket": "divine", "position": "2"}
    values |= {"game_mode": "turbo", "min_sample": "10"}
    assert set(query) == set(values)
    assert (await get(f"{PREFIX}/tier-list", key, **values)).status_code == 200


# CLI


def test_cli_creates_lists_and_revokes_keys(engine, monkeypatch, capsys):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        with Session(engine) as session, session.begin():
            yield session

    monkeypatch.setattr("dota_insights.db.session.session_scope", scope)
    assert cli.main(["create", "my bot", "--per-minute", "5"]) == 0
    out = capsys.readouterr().out
    key = out.strip().splitlines()[-1]
    assert key.startswith("di_") and "per minute: 5" in out
    with Session(engine) as session:
        key_id = session.scalar(select(ApiKey.id))

    assert cli.main(["list"]) == 0
    listing = capsys.readouterr().out
    assert key_id in listing and "active" in listing and key not in listing

    assert cli.main(["revoke", key_id]) == 0
    assert cli.main(["revoke", "nope"]) == 1
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert "revoked" in capsys.readouterr().out


def test_cli_refuses_without_secret(monkeypatch, capsys):
    monkeypatch.setattr(settings, "api_key_secret", None)
    assert cli.main(["list"]) == 2
