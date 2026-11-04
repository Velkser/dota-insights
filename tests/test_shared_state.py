"""Shared state through Redis (p2-07): the KV client, the shared cache and source quotas."""

import json

import httpx
import pytest
from fakes import BrokenKV, FakeKV

from dota_insights import cache, metrics
from dota_insights.cache import cached, clear_all, invalidate_shared
from dota_insights.config import Settings
from dota_insights.kv import KVError, UpstashKV, reset_kv, use_kv
from dota_insights.sources.opendota import OpenDotaClient, OpenDotaRateLimitedError
from dota_insights.sources.quota import QuotaExhaustedError, SharedQuota
from dota_insights.sources.stratz import StratzClient, StratzRateLimitedError


@pytest.fixture
def kv():
    fake = FakeKV()
    use_kv(fake)
    clear_all()
    yield fake
    reset_kv()
    clear_all()


# Upstash REST client


def upstash(handler, **kwargs) -> UpstashKV:
    return UpstashKV(
        "https://redis.example", "secret", transport=httpx.MockTransport(handler), **kwargs
    )


async def test_upstash_sends_commands_as_json_arrays_with_bearer_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["Authorization"], json.loads(request.content)))
        command = json.loads(request.content)
        if command[0] == "EVAL":
            return httpx.Response(200, json={"result": [3, 7]})
        if command[0] == "SET":
            return httpx.Response(200, json={"result": None if "NX" in command else "OK"})
        return httpx.Response(200, json={"result": "v"})

    client = upstash(handler)
    assert await client.get("k") == "v"
    await client.set("k", "v", ex=12.4)
    assert await client.set_nx("k", "v", ex=5) is False
    assert await client.incr_windows([("a", 60), ("b", 86400)]) == [3, 7]
    await client.aclose()
    assert all(auth == "Bearer secret" for auth, _ in seen)
    assert seen[0][1] == ["GET", "k"]
    assert seen[1][1] == ["SET", "k", "v", "EX", "12"]
    assert seen[2][1] == ["SET", "k", "v", "NX", "EX", "5"]
    eval_cmd = seen[3][1]
    assert eval_cmd[0] == "EVAL" and eval_cmd[2:] == ["2", "a", "b", "60", "86400"]


async def test_upstash_failure_raises_and_cools_down():
    calls = []
    now = [0.0]

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "WRONGPASS"})

    client = upstash(handler, cooldown=30, clock=lambda: now[0])
    with pytest.raises(KVError) as info:
        await client.get("k")
    assert "secret" not in str(info.value) and "WRONGPASS" not in str(info.value)
    with pytest.raises(KVError):
        await client.get("k")
    assert len(calls) == 1  # skipped while cooling down
    now[0] = 31
    with pytest.raises(KVError):
        await client.get("k")
    assert len(calls) == 2


async def test_upstash_transport_error_is_kv_error():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(KVError):
        await upstash(handler).get("k")


@pytest.mark.parametrize(
    ("url_var", "token_var"),
    [
        ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
        ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
    ],
)
def test_redis_settings_accept_vercel_integration_names(monkeypatch, url_var, token_var):
    for prefix in ("DOTA_INSIGHTS_REDIS_REST", "UPSTASH_REDIS_REST", "KV_REST_API"):
        for suffix in ("URL", "TOKEN"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    monkeypatch.setenv(url_var, "https://x.upstash.io")
    monkeypatch.setenv(token_var, "t")
    settings = Settings(_env_file=None)
    assert settings.redis_rest_url == "https://x.upstash.io"
    assert settings.redis_rest_token == "t"


# Shared cache


def counting(ttl=60, **kwargs):
    calls = []

    @cached(ttl=ttl, **kwargs)
    async def fetch(hero_id: int, *, bracket: int | None = None):
        calls.append((hero_id, bracket))
        return {"hero_id": hero_id, "games": [1, 2, 3]}

    return fetch, calls


async def test_shared_cache_serves_other_instances(kv):
    fetch, calls = counting(ttl=3600)
    assert await fetch(1, bracket=5) == {"hero_id": 1, "games": [1, 2, 3]}
    stored = [k for k in kv.data if k.startswith("di:cache:")]
    assert len(stored) == 1 and kv.ttls[stored[0]] == 3600
    assert "fetch" in stored[0]

    clear_all()  # a fresh serverless instance: empty memory, same Redis
    with metrics.track_call() as call:
        assert await fetch(1, bracket=5) == {"hero_id": 1, "games": [1, 2, 3]}
    assert calls == [(1, 5)]
    assert call.shared_cache_hits == 1 and call.as_log()["cache_hit"] is True

    await fetch(2)
    assert calls == [(1, 5), (2, None)]


async def test_values_that_do_not_round_trip_stay_local(kv):
    @cached(ttl=60)
    async def versions():
        return (1, 2)  # a tuple would come back from JSON as a list

    @cached(ttl=60)
    async def by_id():
        return {1: "a"}  # int keys would come back as strings

    assert await versions() == (1, 2)
    assert await by_id() == {1: "a"}
    assert not [k for k in kv.data if k.startswith("di:cache:")]


async def test_unencodable_arguments_skip_redis(kv):
    @cached(ttl=60)
    async def fetch(thing):
        return [1]

    assert await fetch(object()) == [1]
    assert not [k for k in kv.data if k.startswith("di:cache:")]


async def test_shared_false_keeps_memory_only(kv):
    fetch, _ = counting(shared=False)
    await fetch(1)
    assert kv.commands == []


async def test_new_generation_invalidates_shared_entries(kv):
    fetch, calls = counting()
    await fetch(1)
    assert await invalidate_shared("new patch 7.40") is True
    clear_all()
    await fetch(1)
    assert calls == [(1, None), (1, None)]
    assert kv.data[cache.GENERATION_KEY] == "1"


async def test_redis_down_falls_back_to_memory():
    broken = BrokenKV()
    use_kv(broken)
    clear_all()
    try:
        fetch, calls = counting()
        assert await fetch(1) == {"hero_id": 1, "games": [1, 2, 3]}
        assert await fetch(1) == {"hero_id": 1, "games": [1, 2, 3]}
        assert calls == [(1, None)]
        assert await invalidate_shared("x") is False
    finally:
        reset_kv()
        clear_all()


async def test_no_redis_means_no_shared_calls():
    use_kv(None)
    try:
        fetch, calls = counting()
        await fetch(1)
        await fetch(1)
        assert calls == [(1, None)]
    finally:
        reset_kv()


# Shared quotas


class FakeTime:
    def __init__(self, now: float = 1_000_000_020.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_quota_is_shared_between_instances(kv):
    clock = FakeTime()
    first = SharedQuota("opendota", per_minute=100, per_day=3, clock=clock, sleep=clock.sleep)
    second = SharedQuota("opendota", per_minute=100, per_day=3, clock=clock, sleep=clock.sleep)
    await first.acquire()
    await second.acquire()
    with metrics.track_call() as call:
        usage = await first.acquire()
    assert usage.day_used == 3 and call.quota_left == {"opendota": 0}
    with pytest.raises(QuotaExhaustedError) as info:
        await second.acquire()
    assert 0 < info.value.retry_after <= 86400
    assert (await first.usage()).day_used == 4


async def test_quota_waits_for_next_minute_when_minute_cap_is_reached(kv):
    clock = FakeTime(now=1_000_000_040.0)  # 20 s into a minute
    quota = SharedQuota("stratz", per_minute=2, per_day=100, clock=clock, sleep=clock.sleep)
    await quota.acquire()
    await quota.acquire()
    await quota.acquire()
    assert len(clock.slept) == 1 and 40 <= clock.slept[0] <= 41


async def test_quota_counts_locally_without_redis():
    use_kv(BrokenKV())
    try:
        clock = FakeTime()
        quota = SharedQuota("opendota", per_minute=10, per_day=1, clock=clock, sleep=clock.sleep)
        await quota.acquire()
        with pytest.raises(QuotaExhaustedError):
            await quota.acquire()
        assert (await quota.usage()).day_used == 2
    finally:
        reset_kv()


async def test_clients_turn_spent_quota_into_rate_limit_errors(kv):
    def handler(request):
        return httpx.Response(200, json={"data": {}})

    transport = httpx.MockTransport(handler)
    clock = FakeTime()
    od_quota = SharedQuota("opendota", per_minute=10, per_day=1, clock=clock, sleep=clock.sleep)
    opendota = OpenDotaClient(transport=transport, quota=od_quota, api_key=None)
    with metrics.track_call() as call:
        await opendota.heroes()
    assert call.source_calls == {"opendota": 1}
    with pytest.raises(OpenDotaRateLimitedError) as info:
        await opendota.heroes()
    assert info.value.retry_after > 3600

    st_quota = SharedQuota("stratz", per_minute=10, per_day=0, clock=clock, sleep=clock.sleep)
    stratz = StratzClient(transport=transport, quota=st_quota, token="t")
    with pytest.raises(StratzRateLimitedError):
        await stratz.query("{ x }")
    await opendota.aclose()
    await stratz.aclose()
