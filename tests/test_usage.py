"""Usage analytics (p4-08), feedback (p5-02) and unmet questions (p5-03).

Calls go through the real middleware stack (in process or over the ASGI app), events through
a fake Redis queue, and the collector's flush, the retention and the report run on SQLite.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fakes import BrokenKV, FakeKV
from fastmcp import Client
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dota_insights.collector.context import CollectorContext
from dota_insights.collector.jobs import JOBS
from dota_insights.collector.scheduler import run_job
from dota_insights.config import settings
from dota_insights.db.access import use_database
from dota_insights.db.models import Base, UsageEvent, UsageFeedback
from dota_insights.kv import KV, reset_kv, use_kv
from dota_insights.mcp.asgi import app
from dota_insights.mcp.ratelimit import FEEDBACK_TOOLS, limits_for
from dota_insights.mcp.server import mcp
from dota_insights.usage import events, store
from dota_insights.usage.report import build_report, format_report, percentile

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
LOOKUP = {"query": "PA", "kind": "hero"}


@pytest.fixture
def kv():
    fake = FakeKV()
    use_kv(fake)
    events._salts.clear()
    store._last_calls.clear()
    yield fake
    reset_kv()
    events._salts.clear()


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def queued(kv: FakeKV) -> list[dict]:
    return [json.loads(item) for item in kv.lists.get(store.QUEUE_KEY, [])]


async def call(tool: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return result.structured_content


async def call_over_http(tool: str, arguments: dict, headers: dict | None = None) -> dict:
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
            headers={"Accept": "application/json, text/event-stream", **(headers or {})},
        )
    return response.json()["result"]["structuredContent"]


# What is stored


def test_argument_shape_keeps_names_and_drops_people_and_free_text():
    shaped = events.shape_arguments(
        {
            "hero": "Anti-Mage",
            "items": ["Black King Bar", "Blink Dagger"],
            "player": "56939869",
            "player_a": "https://www.opendota.com/players/1",
            "what_was_wrong": "my friend Bob says so",
            "query": "x" * 500,
            "spec": {"filters": {"patch": "7.41", "player": "123"}},
            "matches": 100,
            "days": None,
        }
    )
    assert shaped["hero"] == "Anti-Mage"
    assert shaped["items"] == ["Black King Bar", "Blink Dagger"]
    assert shaped["player"] == shaped["player_a"] == "<player>"
    assert shaped["what_was_wrong"] == "<text: 21 chars>"
    assert len(shaped["query"]) == events.MAX_STRING
    assert shaped["spec"] == {"filters": {"patch": "7.41", "player": "<player>"}}
    assert shaped["matches"] == 100 and shaped["days"] is None


async def test_client_hash_is_stable_within_a_week_and_rotates(kv):
    monday, sunday = datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 8, 23, tzinfo=UTC)
    first = await events.client_hash("203.0.113.7", monday)
    assert await events.client_hash("203.0.113.7", sunday) == first
    assert await events.client_hash("198.51.100.2", sunday) != first
    events._salts.clear()  # another instance shares the salt through Redis
    assert await events.client_hash("203.0.113.7", sunday) == first
    assert await events.client_hash("203.0.113.7", monday + timedelta(days=7)) != first
    assert len(first) == 16
    assert not [k for k, v in kv.data.items() if "203.0.113.7" in k + v]


async def test_tool_call_over_http_queues_one_event_without_the_ip(kv):
    agent = {"x-real-ip": "203.0.113.7", "user-agent": "claude-desktop/1.2"}
    assert (await call_over_http("lookup_reference", LOOKUP, agent))["status"] == "ok"
    [event] = queued(kv)
    assert event["type"] == "call" and event["tool"] == "lookup_reference"
    assert event["arguments"] == LOOKUP
    assert event["status"] == "ok" and event["error_kind"] is None and event["message"] is None
    assert event["latency_ms"] >= 0 and event["sources"] == [] and event["client_name"]
    assert event["client_hash"] == await events.client_hash("203.0.113.7")
    assert "203.0.113.7" not in json.dumps(kv.data) + json.dumps(kv.lists)
    # The call is remembered for linking feedback.
    assert await store.last_call(event["client_hash"], "lookup_reference") == event["event_id"]


async def test_non_ok_calls_keep_status_error_kind_and_message(kv):
    await call("lookup_reference", {"query": "zzzzzz", "kind": "hero"})
    await call("get_player_profile", {"player": "not a player"})
    insufficient, failed = queued(kv)
    assert insufficient["status"] in {"insufficient_data", "error"}
    assert failed["status"] == "error" and failed["error_kind"] == "error_envelope"
    assert failed["message"] and failed["arguments"] == {"player": "<player>"}
    assert failed["client_hash"] is None  # in process: no HTTP request, no client


async def test_rate_limited_calls_are_recorded_as_such(kv, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    me = {"x-real-ip": "203.0.113.9"}
    await call_over_http("lookup_reference", LOOKUP, me)
    await call_over_http("lookup_reference", LOOKUP, me)
    assert [e["error_kind"] for e in queued(kv)] == [None, "rate_limited"]


class SlowKV(FakeKV):
    async def push_capped(self, key, value, *, max_len):
        await asyncio.sleep(5)


@pytest.mark.parametrize("broken", [BrokenKV, SlowKV])
async def test_recording_never_fails_or_slows_a_call(broken, monkeypatch):
    use_kv(broken())
    monkeypatch.setattr(settings, "usage_write_timeout", 0.05)
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        out = await call_over_http("lookup_reference", LOOKUP, {"x-real-ip": "203.0.113.7"})
        assert out["status"] == "ok"
        assert loop.time() - start < 2
    finally:
        reset_kv()


async def test_usage_can_be_turned_off(kv, monkeypatch):
    monkeypatch.setattr(settings, "usage_enabled", False)
    await call("lookup_reference", LOOKUP)
    assert queued(kv) == []


async def test_without_redis_events_go_straight_to_the_database(engine):
    use_kv(None)
    try:
        with use_database(engine):
            await call("lookup_reference", LOOKUP)
        with Session(engine) as session:
            assert session.scalar(select(UsageEvent.tool)) == "lookup_reference"
    finally:
        reset_kv()


async def test_queue_is_capped(kv, monkeypatch):
    monkeypatch.setattr(settings, "usage_queue_max", 2)
    for n in range(3):
        assert await store.enqueue({"type": "call", "event_id": str(n)})
    assert [e["event_id"] for e in queued(kv)] == ["1", "2"]


# Flush and retention (the collector's usage job)


def event(n: int, *, ts: datetime = NOW, **fields) -> dict:
    row = {
        "type": "call",
        "event_id": f"e{n}",
        "ts": ts.isoformat(),
        "tool": "get_hero_overview",
        "arguments": {"hero": "Pudge"},
        "status": "ok",
        "error_kind": None,
        "message": None,
        "latency_ms": 100,
        "sources": ["opendota"],
        "cache_hit": False,
        "client_name": "claude-ai",
        "client_version": "1.0",
        "client_hash": "c1",
    }
    return row | fields


async def test_flush_moves_the_queue_in_batches_and_is_idempotent(kv, factory):
    for n in range(7):
        await store.enqueue(event(n))
    kv.lists[store.QUEUE_KEY] += ["not json", "[1]", json.dumps({"type": "call", "ts": "x"})]
    assert await store.flush_queue(kv, factory, batch=3) == 7
    assert kv.lists[store.QUEUE_KEY] == []
    # A crash between insert and trim replays the batch: duplicates are skipped.
    for n in range(2):
        await store.enqueue(event(n))
    await store.flush_queue(kv, factory)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(UsageEvent)) == 7


async def test_usage_job_flushes_and_applies_retention(kv, factory, monkeypatch):
    old = NOW - timedelta(days=settings.usage_retention_days + 1)
    await store.enqueue(event(1, ts=old))
    await store.enqueue(event(2))
    await store.enqueue(
        {
            "type": "feedback",
            "feedback_id": "f1",
            "ts": (NOW - timedelta(days=400)).isoformat(),
            "kind": "feature_request",
            "text": "old request",
        }
    )
    ctx = CollectorContext(factory, budget=None, opendota=None, clock=lambda: NOW)  # type: ignore[arg-type]
    result = await run_job(ctx, JOBS["usage"])
    assert result.status == "ok", result.detail
    assert "flushed=3" in result.detail and "pruned_calls=1 pruned_feedback=1" in result.detail
    with factory() as session:
        assert list(session.scalars(select(UsageEvent.event_id))) == ["e2"]
        assert session.scalar(select(func.count()).select_from(UsageFeedback)) == 0


# Report


def seed(factory) -> None:
    rows = [
        event(1, latency_ms=100, client_hash="a"),
        event(2, latency_ms=200, client_hash="a"),
        event(3, latency_ms=300, client_hash="b", status="error", error_kind="TimeoutError",
              message="The tool timed out."),
        event(4, tool="get_tier_list", latency_ms=50, client_hash="b", status="insufficient_data",
              arguments={"position": 1}, message="Too few matches."),
        event(5, tool="get_tier_list", client_hash="c", status="insufficient_data",
              arguments={"position": 1}, message="Too few matches.",
              ts=NOW - timedelta(days=8)),
        event(6, status="error", error_kind="rate_limited", client_hash="d"),
        event(7, ts=NOW - timedelta(days=40)),
        {"type": "feedback", "feedback_id": "f1", "ts": NOW.isoformat(), "kind": "wrong_answer",
         "tool": "get_hero_overview", "call_id": "e2", "text": "Win rate is off",
         "expected": "51%", "client_name": "claude-ai", "client_hash": "a"},
        {"type": "feedback", "feedback_id": "f2", "ts": NOW.isoformat(),
         "kind": "feature_request", "text": "My exact MMR", "expected": "no MMR tool"},
    ]  # fmt: skip
    with factory.begin() as session:
        store.store_events(session, rows)


def test_report(factory):
    seed(factory)
    with factory() as session:
        report = build_report(session, NOW, days=30)
    totals = report["totals"]
    assert totals["calls"] == 6 and totals["unique_clients"] == 4
    assert totals["success_rate"] == round(2 / 6, 4)
    assert totals["wrong_answer_reports"] == 1 and totals["feature_requests"] == 1
    assert [d["period"] for d in report["per_day"]] == ["2026-10-27", "2026-11-04"]
    assert report["per_day"][1]["calls"] == 5 and report["per_day"][1]["unique_clients"] == 3
    assert [w["period"] for w in report["per_week"]] == ["2026-W44", "2026-W45"]
    overview = report["tools"][0]
    assert report["top_tools"] == ["get_hero_overview", "get_tier_list"]
    assert overview["calls"] == 4 and overview["error"] == 2 and overview["success_rate"] == 0.5
    assert (overview["p50_ms"], overview["p95_ms"]) == (100.0, 300.0)
    [tier, timeout] = report["top_unanswered"]  # rate limiting is not an unanswered question
    assert tier["tool"] == "get_tier_list" and tier["count"] == 2
    assert tier["arguments"] == {"position": 1} and tier["message"] == "Too few matches."
    assert timeout["error_kind"] == "TimeoutError"
    [wrong] = report["wrong_answers"]
    assert wrong["expected"] == "51%" and wrong["call"]["event_id"] == "e2"
    assert report["feature_requests"][0]["why_no_tool_fits"] == "no MMR tool"
    assert report["feedback_by_tool"] == {"get_hero_overview": 1}
    text = format_report(report | {"queued": 3})
    assert "get_tier_list" in text and "My exact MMR" in text and "queue" in text


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([5, 1, 3], 50) == 3
    assert percentile(list(range(1, 101)), 95) == 95


def test_usage_cli_report(factory, engine, monkeypatch, capsys):
    from dota_insights.db import session as db_session
    from dota_insights.usage import cli

    seed(factory)
    monkeypatch.setattr(db_session, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    assert cli.main(["usage", "report", "--json", "--days", "7"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["totals"]["calls"] == 5 and report["queued"] is None


# /admin/usage


async def admin(path: str = "/admin/usage", headers: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers or {})


async def test_admin_route_is_hidden_without_or_with_a_wrong_token(monkeypatch):
    assert (await admin(headers={"Authorization": "Bearer x"})).status_code == 404
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    assert (await admin()).status_code == 404
    assert (await admin(headers={"Authorization": "Bearer wrong"})).status_code == 404


async def test_admin_route_returns_the_report(monkeypatch, engine, factory, kv):
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    monkeypatch.setattr(events, "now_utc", lambda: NOW)
    seed(factory)
    await store.enqueue(event(99))
    auth = {"Authorization": "Bearer s3cret"}
    assert (await admin(headers=auth)).status_code == 503  # no database configured
    with use_database(engine):
        response = await admin("/admin/usage?days=7", auth)
        assert (await admin("/admin/usage?days=x", auth)).status_code == 400
    assert response.status_code == 200
    body = response.json()
    assert body["window"]["days"] == 7 and body["totals"]["calls"] == 5
    assert body["queued"] == 1
    assert response.headers["cache-control"] == "no-store"


# Feedback and feature requests


async def test_wrong_answer_is_linked_to_the_clients_last_call(kv):
    me = {"x-real-ip": "203.0.113.7"}
    await call_over_http("lookup_reference", LOOKUP, me)
    out = await call_over_http(
        "report_wrong_answer",
        {
            "tool_name": "lookup_reference",
            "what_was_wrong": "Phantom Assassin's base armor is wrong.",
            "expected_answer": "1",
        },
        me,
    )
    assert out["status"] == "ok" and out["result"]["linked_to_recent_call"] is True
    lookup, feedback, report_call = queued(kv)
    assert feedback["type"] == "feedback" and feedback["kind"] == "wrong_answer"
    assert feedback["call_id"] == lookup["event_id"] and feedback["expected"] == "1"
    assert feedback["feedback_id"] == out["result"]["feedback_id"]
    # The report call itself is logged without its free text.
    assert report_call["arguments"]["what_was_wrong"].startswith("<text:")
    # Another client's report is not linked to this client's call.
    other = await call_over_http(
        "report_wrong_answer",
        {"tool_name": "lookup_reference", "what_was_wrong": "Wrong."},
        {"x-real-ip": "198.51.100.2"},
    )
    assert other["result"]["linked_to_recent_call"] is False


async def test_wrong_answer_needs_a_real_tool_and_storage(kv):
    out = await call("report_wrong_answer", {"tool_name": "nope", "what_was_wrong": "bad"})
    assert out["status"] == "error" and "nope" in out["message"]
    use_kv(BrokenKV())
    out = await call("report_wrong_answer", {"tool_name": "server_info", "what_was_wrong": "bad"})
    assert out["status"] == "error" and out["hint"]


async def test_suggest_feature_is_stored(kv, factory):
    out = await call(
        "suggest_feature",
        {"question": "What is my exact MMR?", "why_no_tool_fits": "MMR is private"},
    )
    assert out["status"] == "ok" and out["result"]["linked_to_recent_call"] is False
    await store.flush_queue(kv, factory)
    with factory() as session:
        row = session.scalars(select(UsageFeedback)).one()
    assert (row.kind, row.tool, row.text, row.expected) == (
        "feature_request",
        None,
        "What is my exact MMR?",
        "MMR is private",
    )


def test_feedback_tools_have_their_own_low_limits():
    for tool in FEEDBACK_TOOLS:
        scopes = {(limit.scope, limit.window) for limit in limits_for(tool)}
        assert ("feedback", 60) in scopes and ("feedback", 86400) in scopes
    assert all(limit.scope != "feedback" for limit in limits_for("lookup_reference"))


async def test_feedback_rate_limit_applies_per_client(kv, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_feedback_per_minute", 1)
    args = {"question": "Which hero has the best smile?"}
    me = {"x-real-ip": "203.0.113.7"}
    assert (await call_over_http("suggest_feature", args, me))["status"] == "ok"
    refused = await call_over_http("suggest_feature", args, me)
    assert refused["status"] == "error" and "1 reports a minute" in refused["message"]


def test_fake_kv_implements_every_kv_operation():
    assert not getattr(FakeKV, "__abstractmethods__", None)
    assert KV.__abstractmethods__ >= {"push_capped", "list_head", "list_drop", "list_len"}
