import asyncio
import json

import httpx
import pytest

from dota_insights import sources
from dota_insights.cache import cached, clear_all
from dota_insights.mcp.tools import discover_domains
from dota_insights.sources.stratz import (
    StratzAuthError,
    StratzClient,
    StratzQueryError,
)


async def _no_sleep(_: float) -> None:
    return None


def test_discovers_meta_domain():
    names = [m.__name__.rsplit(".", 1)[-1] for m in discover_domains()]
    assert "meta" in names
    assert names == sorted(names)


async def test_cached_reuses_result_within_ttl():
    now = [0.0]
    calls = []

    @cached(ttl=10, clock=lambda: now[0])
    async def fetch(x):
        calls.append(x)
        return x * 2

    assert await fetch(2) == 4
    assert await fetch(2) == 4
    assert calls == [2]
    now[0] = 11
    assert await fetch(2) == 4
    assert calls == [2, 2]
    fetch.cache_clear()
    await fetch(2)
    assert calls == [2, 2, 2]


async def test_cached_shares_inflight_call_and_does_not_cache_errors():
    calls = 0

    @cached(ttl=60)
    async def slow():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "v"

    assert await asyncio.gather(slow(), slow(), slow()) == ["v", "v", "v"]
    assert calls == 1

    failures = 0

    @cached(ttl=60)
    async def flaky():
        nonlocal failures
        failures += 1
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await flaky()
    assert failures == 2
    clear_all()


def _stratz(handler) -> StratzClient:
    return StratzClient(token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep)


async def test_stratz_sends_auth_and_returns_data():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["ua"] = request.headers["User-Agent"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"constants": {"hero": {"id": 1}}}})

    async with _stratz(handler) as client:
        data = await client.query("query($id: Short!) { x }", {"id": 1})
    assert data == {"constants": {"hero": {"id": 1}}}
    assert seen["url"] == "https://api.stratz.com/graphql"
    assert seen["auth"] == "Bearer t"
    assert seen["ua"] == "STRATZ_API"
    assert seen["body"]["variables"] == {"id": 1}


async def test_stratz_retries_then_succeeds_and_maps_errors():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"errors": [{"message": "bad field"}]})

    async with _stratz(handler) as client:
        with pytest.raises(StratzQueryError, match="bad field"):
            await client.query("{ x }")
    assert attempts == 2

    async with _stratz(lambda r: httpx.Response(403)) as client:
        with pytest.raises(StratzAuthError):
            await client.query("{ x }")

    async with StratzClient(token=None) as client:
        with pytest.raises(StratzAuthError, match="not configured"):
            await client.query("{ x }")


async def test_shared_clients_can_be_replaced():
    fake = StratzClient(token="x")
    sources.use_clients(stratz=fake)
    assert sources.get_stratz() is fake
    await sources.close_clients()
    assert sources.get_stratz() is not fake
    await sources.close_clients()
