"""Rate limiting (p4-02) and monitoring (p4-03): client limits, call metrics, alerts, /status."""

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from fakes import FakeKV
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dota_insights.cache import cached, clear_all
from dota_insights.config import settings
from dota_insights.db.models import AggregateStatus, Base, CollectorCursor, CollectorJobRun, Patch
from dota_insights.kv import reset_kv, use_kv
from dota_insights.logging import TOOL_CALL_LOGGER, ToolCallLoggingMiddleware, setup_logging
from dota_insights.mcp import status as status_module
from dota_insights.mcp.asgi import app
from dota_insights.mcp.ratelimit import HEAVY_TOOLS, RateLimiter
from dota_insights.mcp.server import mcp
from dota_insights.metrics import record_quota, record_source_call
from dota_insights.sources.quota import QuotaUsage
from dota_insights.telegram import alerts
from dota_insights.telegram.alerts import Alerter, use_alerter


@pytest.fixture
def kv():
    fake = FakeKV()
    use_kv(fake)
    yield fake
    reset_kv()


async def call_over_http(tool: str, arguments: dict, headers: dict) -> dict:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={"Accept": "application/json, text/event-stream", **headers},
        )
    return response.json()["result"]


# Rate limiting


async def test_client_over_limit_gets_error_envelope_with_reset_seconds(kv, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    args = {"query": "PA", "kind": "hero"}
    me = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
    for _ in range(2):
        result = await call_over_http("lookup_reference", args, me)
        assert result["structuredContent"]["status"] == "ok"
    refused = (await call_over_http("lookup_reference", args, me))["structuredContent"]
    assert refused["status"] == "error"
    assert "2 tool calls a minute" in refused["message"]
    assert "seconds" in refused["hint"]
    # Another address is counted separately; raw IPs are not stored.
    other = await call_over_http("lookup_reference", args, {"x-real-ip": "198.51.100.2"})
    assert other["structuredContent"]["status"] == "ok"
    assert not [k for k in kv.data if "203.0.113.7" in k]


async def test_rate_limits_are_off_when_disabled(kv):
    assert settings.rate_limit_enabled is False  # tests/conftest.py
    for _ in range(3):
        result = await call_over_http(
            "lookup_reference", {"query": "PA", "kind": "hero"}, {"x-real-ip": "1.2.3.4"}
        )
        assert result["structuredContent"]["status"] == "ok"
    assert kv.commands.count("EVAL") == 0


async def test_heavy_tools_have_their_own_lower_limit(kv, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_heavy_per_minute", 1)
    limiter = RateLimiter(clock=lambda: 1_000_000_010.0)  # 50 s into a minute
    assert await limiter.hit("c", "explore_query") is None
    over = await limiter.hit("c", "explore_query")
    assert over is not None
    limit, reset = over
    assert limit.scope == "heavy" and reset == pytest.approx(10.0)
    assert await limiter.hit("c", "lookup_reference") is None  # light tools still allowed


async def test_rate_limiter_counts_locally_without_redis(monkeypatch):
    use_kv(None)
    try:
        monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
        limiter = RateLimiter(clock=lambda: 1_000_000_010.0)
        assert await limiter.hit("c", "lookup_reference") is None
        assert await limiter.hit("c", "lookup_reference") is not None
    finally:
        reset_kv()


async def test_heavy_tool_names_are_registered_tools():
    names = {tool.name for tool in await mcp.list_tools()}
    assert names >= HEAVY_TOOLS, HEAVY_TOOLS - names


# Call metrics


async def test_tool_call_log_carries_metrics(caplog):
    server = FastMCP("t")
    server.add_middleware(ToolCallLoggingMiddleware())

    @cached(ttl=60, shared=False)
    async def fetch():
        record_source_call("opendota")
        record_quota("opendota", 1234)
        return 1

    @server.tool
    async def probe() -> int:
        return await fetch()

    clear_all()
    caplog.set_level(logging.INFO, logger=TOOL_CALL_LOGGER)
    async with Client(server) as client:
        await client.call_tool("probe", {})
        await client.call_tool("probe", {})
    first, second = [r for r in caplog.records if r.name == TOOL_CALL_LOGGER]
    assert first.source_calls == {"opendota": 1} and first.sources == ["opendota"]
    assert first.cache_misses == 1 and first.cache_hit is False
    assert first.quota_left == {"opendota": 1234}
    assert second.cache_hit is True and second.source_calls == {}
    assert isinstance(second.duration_ms, float) and second.status == "ok"


# Telegram alerts


class FakeTelegram:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(self.status, json={"ok": self.status == 200})


def alerter(telegram: FakeTelegram, now: float = 1_000_000_000.0) -> Alerter:
    return Alerter(
        "123:SECRET",
        "42",
        throttle=1800,
        transport=httpx.MockTransport(telegram),
        clock=lambda: now,
    )


async def test_alerts_are_sent_and_throttled_across_instances(kv):
    telegram = FakeTelegram()
    assert await alerter(telegram).alert("x", "hello") is True
    assert await alerter(telegram).alert("x", "hello again") is False  # another instance
    assert len(telegram.sent) == 1
    path, body = telegram.sent[0]
    assert path == "/bot123:SECRET/sendMessage"
    assert body == {"chat_id": "42", "text": "[dota-insights] hello"}


async def test_alert_failure_is_logged_without_token(kv, caplog):
    caplog.set_level(logging.WARNING)
    assert await alerter(FakeTelegram(status=500)).alert("y", "boom") is False
    assert "telegram alert failed" in caplog.text
    assert "SECRET" not in caplog.text


def test_setup_logging_quiets_httpx_request_lines():
    # httpx logs request URLs at INFO, and the Telegram token is part of the URL.
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        setup_logging("INFO")
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


async def test_error_rate_spike_alerts_once(kv, monkeypatch):
    monkeypatch.setattr(settings, "alert_error_min_calls", 4)
    monkeypatch.setattr(settings, "alert_error_rate", 0.5)
    telegram = FakeTelegram()
    monitor = alerter(telegram)
    await monitor.record_call(failed=False)
    await monitor.record_call(failed=True)
    assert telegram.sent == []  # 3 calls: below the minimum
    await monitor.record_call(failed=True)
    await monitor.record_call(failed=True)  # throttled
    assert len(telegram.sent) == 1
    assert "3 of the last 4 tool calls failed" in telegram.sent[0][1]["text"]


async def test_quota_near_exhaustion_alerts(kv, monkeypatch):
    monkeypatch.setattr(settings, "alert_quota_ratio", 0.8)
    telegram = FakeTelegram()
    monitor = alerter(telegram)
    use_alerter(monitor)
    try:
        await alerts._quota_observer(QuotaUsage("opendota", 1, 50, 100, 2500))
        await alerts._quota_observer(QuotaUsage("opendota", 1, 50, 2000, 2500))
        await alerts._quota_observer(QuotaUsage("opendota", 1, 50, 2001, 2500))
        await alerts._quota_observer(QuotaUsage("opendota", 1, 50, 2501, 2500))
    finally:
        use_alerter(None)
    texts = [body["text"] for _, body in telegram.sent]
    assert len(texts) == 2
    assert "nearly spent: 2000 of 2500" in texts[0] and "is spent" in texts[1]


# /status


async def test_status_route_reports_quotas_without_database(kv, monkeypatch):
    monkeypatch.setattr(settings, "database_url", None)
    status_module.reset_status_cache()
    kv.data["di:quota:opendota:d:" + str(int(datetime.now(UTC).timestamp() // 86400))] = "17"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["shared_state"] == "redis" and body["quotas_scope"] == "all instances"
    assert body["quotas"]["opendota"]["day_used"] == 17
    assert body["quotas"]["opendota"]["day_left"] == settings.opendota_server_daily - 17
    assert body["data"] == {"configured": False}
    assert body["alerts"] == {"telegram": False}


def test_collector_freshness_reads_last_runs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'status.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    t = lambda h: datetime(2026, 11, 4, h, tzinfo=UTC)  # noqa: E731
    with factory.begin() as session:
        session.add(Patch(patch_id=1, name="7.40", released_at=t(0)))
        session.add_all(
            [
                CollectorJobRun(job="enrich", started_at=t(1), finished_at=t(1), status="ok"),
                CollectorJobRun(job="enrich", started_at=t(2), status="failed", api_calls=3),
                CollectorJobRun(job="patches", started_at=t(3), finished_at=t(3), status="ok"),
            ]
        )
        session.add(
            CollectorCursor(
                stream="public_matches:5",
                newest_match_id=1,
                newest_start_time=t(4),
                updated_at=t(4),
            )
        )
        session.add(AggregateStatus(patch_id=1, stale=True, rebuilt_at=t(2)))
    data = status_module.collector_freshness(factory)
    engine.dispose()
    assert data["jobs"]["enrich"]["status"] == "failed"
    assert data["jobs"]["enrich"]["api_calls"] == 3
    assert data["jobs"]["enrich"]["last_ok_at"].startswith("2026-11-04T01")
    assert data["jobs"]["patches"]["status"] == "ok"
    assert data["newest_match_start"].startswith("2026-11-04T04")
    assert data["aggregates"]["stale_patches"] == 1
