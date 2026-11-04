from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from dota_insights.sources import opendota
from dota_insights.sources.opendota import (
    OpenDotaClient,
    OpenDotaError,
    OpenDotaNotFoundError,
    OpenDotaRateLimitedError,
    OpenDotaUnavailableError,
    TokenBucket,
    _parse_retry_after,
)


class FakeTime:
    """A fake monotonic clock whose sleep advances time instantly and records each delay."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, fake: FakeTime | None = None, **kwargs) -> OpenDotaClient:
    fake = fake or FakeTime()
    kwargs.setdefault("api_key", None)
    kwargs.setdefault("requests_per_minute", 6000)  # keep limiter waits out of retry tests
    return OpenDotaClient(
        transport=httpx.MockTransport(handler),
        clock=fake.clock,
        sleep=fake.sleep,
        rng=lambda: 1.0,
        **kwargs,
    )


def recorder(responses: list[httpx.Response]) -> tuple[list[httpx.Request], Handler]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses[min(len(requests), len(responses)) - 1]

    return requests, handler


async def test_builds_url_and_params_with_api_key():
    requests, handler = recorder([httpx.Response(200, json=[{"match_id": 1}])])
    async with make_client(handler, api_key="secret") as client:
        result = await client.player_matches(
            123, limit=5, win=True, hero_id=None, project=["hero_id", "kills"]
        )

    assert result == [{"match_id": 1}]
    url = requests[0].url
    assert url.host == "api.opendota.com"
    assert url.path == "/api/players/123/matches"
    assert url.params["limit"] == "5"
    assert url.params["win"] == "1"
    assert "hero_id" not in url.params
    assert url.params.get_list("project") == ["hero_id", "kills"]
    assert url.params["api_key"] == "secret"


async def test_no_api_key_param_when_unset():
    requests, handler = recorder([httpx.Response(200, json=[])])
    async with make_client(handler) as client:
        await client.public_matches(min_rank=70)

    assert requests[0].url.path == "/api/publicMatches"
    assert dict(requests[0].url.params) == {"min_rank": "70"}


async def test_api_key_defaults_to_settings(monkeypatch):
    monkeypatch.setattr(opendota.settings, "opendota_api_key", "from-settings")
    requests, handler = recorder([httpx.Response(200, json=[])])
    async with OpenDotaClient(transport=httpx.MockTransport(handler)) as client:
        await client.heroes()

    assert requests[0].url.params["api_key"] == "from-settings"


@pytest.mark.parametrize(
    ("call", "method", "path", "params"),
    [
        (lambda c: c.hero_stats(), "GET", "/api/heroStats", {}),
        (lambda c: c.heroes(), "GET", "/api/heroes", {}),
        (lambda c: c.match(7), "GET", "/api/matches/7", {}),
        (lambda c: c.request_parse(7), "POST", "/api/request/7", {}),
        (lambda c: c.player(9), "GET", "/api/players/9", {}),
        (lambda c: c.player_heroes(9), "GET", "/api/players/9/heroes", {}),
        (lambda c: c.pro_matches(less_than_match_id=5), "GET", "/api/proMatches",
         {"less_than_match_id": "5"}),
        (lambda c: c.benchmarks(1), "GET", "/api/benchmarks", {"hero_id": "1"}),
        (lambda c: c.hero_matchups(1), "GET", "/api/heroes/1/matchups", {}),
        (lambda c: c.hero_item_popularity(1), "GET", "/api/heroes/1/itemPopularity", {}),
        (lambda c: c.constants("items"), "GET", "/api/constants/items", {}),
        (lambda c: c.explorer("select 1"), "GET", "/api/explorer", {"sql": "select 1"}),
        (lambda c: c.live(), "GET", "/api/live", {}),
    ],
)  # fmt: skip
async def test_endpoint_paths(call, method, path, params):
    requests, handler = recorder([httpx.Response(200, json={})])
    async with make_client(handler) as client:
        await call(client)

    assert requests[0].method == method
    assert requests[0].url.path == path
    assert dict(requests[0].url.params) == params


async def test_retries_429_then_succeeds_with_backoff():
    fake = FakeTime()
    requests, handler = recorder(
        [httpx.Response(429), httpx.Response(429), httpx.Response(200, json={"ok": True})]
    )
    async with make_client(handler, fake, backoff_base=1.0) as client:
        assert await client.match(1) == {"ok": True}

    assert len(requests) == 3
    # rng=1.0 gives the upper end of the jitter window: base * 2**attempt
    assert fake.sleeps == [1.0, 2.0]


async def test_retry_after_seconds_is_honored():
    fake = FakeTime()
    requests, handler = recorder(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=[])]
    )
    async with make_client(handler, fake) as client:
        await client.heroes()

    assert len(requests) == 2
    assert fake.sleeps == [7.0]


async def test_retry_after_is_capped():
    fake = FakeTime()
    _, handler = recorder(
        [httpx.Response(503, headers={"Retry-After": "9999"}), httpx.Response(200, json=[])]
    )
    async with make_client(handler, fake, retry_after_max=60) as client:
        await client.heroes()

    assert fake.sleeps == [60]


def test_parse_retry_after_http_date():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=30), usegmt=True)
    assert _parse_retry_after(header, now=now) == 30
    assert _parse_retry_after("garbage") is None
    assert _parse_retry_after(None) is None


async def test_retries_5xx_and_connection_errors():
    fake = FakeTime()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        if calls == 2:
            return httpx.Response(502)
        return httpx.Response(200, json={"ok": 1})

    async with make_client(handler, fake) as client:
        assert await client.live() == {"ok": 1}
    assert calls == 3


async def test_404_maps_to_not_found_without_retry():
    requests, handler = recorder([httpx.Response(404, json={"error": "Not Found"})])
    async with make_client(handler) as client:
        with pytest.raises(OpenDotaNotFoundError) as info:
            await client.match(1)

    assert info.value.status_code == 404
    assert len(requests) == 1


async def test_persistent_429_raises_rate_limited():
    requests, handler = recorder([httpx.Response(429, headers={"Retry-After": "1"})])
    async with make_client(handler, max_retries=2) as client:
        with pytest.raises(OpenDotaRateLimitedError) as info:
            await client.heroes()

    assert info.value.retry_after == 1
    assert len(requests) == 3


async def test_persistent_5xx_raises_unavailable():
    requests, handler = recorder([httpx.Response(500)])
    async with make_client(handler, max_retries=1) as client:
        with pytest.raises(OpenDotaUnavailableError) as info:
            await client.heroes()

    assert info.value.status_code == 500
    assert len(requests) == 2


async def test_persistent_connection_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    async with make_client(handler, max_retries=1) as client:
        with pytest.raises(OpenDotaUnavailableError):
            await client.heroes()


async def test_other_4xx_raises_base_error_and_hides_api_key():
    requests, handler = recorder([httpx.Response(400)])
    async with make_client(handler, api_key="secret") as client:
        with pytest.raises(OpenDotaError) as info:
            await client.explorer("bad sql")

    assert type(info.value) is OpenDotaError
    assert info.value.status_code == 400
    assert "secret" not in str(info.value)
    assert len(requests) == 1


async def test_limiter_spaces_requests():
    fake = FakeTime()
    _, handler = recorder([httpx.Response(200, json=[])])
    async with make_client(handler, fake, requests_per_minute=30) as client:
        for _ in range(3):
            await client.heroes()

    # 30/min with a burst of 1: first request is immediate, then one every 2 seconds.
    assert fake.sleeps == pytest.approx([2.0, 2.0])
    assert fake.now == pytest.approx(4.0)


async def test_token_bucket_burst_then_refill():
    fake = FakeTime()
    bucket = TokenBucket(60, capacity=3, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert fake.sleeps == []

    await bucket.acquire()
    assert fake.sleeps == pytest.approx([1.0])

    fake.now += 10  # idle time refills only up to capacity
    for _ in range(3):
        await bucket.acquire()
    assert fake.sleeps == pytest.approx([1.0])


def test_token_bucket_rejects_bad_config():
    with pytest.raises(ValueError):
        TokenBucket(0)
    with pytest.raises(ValueError):
        TokenBucket(60, capacity=0.5)
