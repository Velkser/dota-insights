"""Async client for the OpenDota REST API (https://docs.opendota.com/).

The client applies a token-bucket rate limit shared by all requests it makes and retries
transient failures (HTTP 429, 5xx and connection errors) with exponential backoff and jitter,
honoring ``Retry-After`` when the server sends it.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self, TypedDict

import httpx

from dota_insights.config import settings

BASE_URL = "https://api.opendota.com/api"

# The free tier allows 60 requests/minute; stay a little under it by default.
DEFAULT_REQUESTS_PER_MINUTE = 50.0

_UNSET: Any = object()

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class OpenDotaError(Exception):
    """Base error for OpenDota API failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenDotaNotFoundError(OpenDotaError):
    """The requested resource does not exist (HTTP 404)."""


class OpenDotaRateLimitedError(OpenDotaError):
    """OpenDota kept answering HTTP 429 after all retries."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class OpenDotaUnavailableError(OpenDotaError):
    """OpenDota kept failing with 5xx or connection errors after all retries."""


class Hero(TypedDict, total=False):
    id: int
    name: str
    localized_name: str
    primary_attr: str
    attack_type: str
    roles: list[str]
    legs: int


class HeroMatchup(TypedDict, total=False):
    hero_id: int
    games_played: int
    wins: int


class ParseJob(TypedDict, total=False):
    jobId: int


class ParseRequest(TypedDict, total=False):
    job: ParseJob


class TokenBucket:
    """Token-bucket limiter: ``rate_per_minute`` sustained, bursts of up to ``capacity``."""

    def __init__(
        self,
        rate_per_minute: float,
        capacity: float = 1.0,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
        self._updated = now

    async def acquire(self) -> None:
        """Wait until a token is available, then take it."""
        async with self._lock:
            self._refill()
            while self._tokens < 1:
                await self._sleep((1 - self._tokens) / self.rate_per_second)
                self._refill()
            self._tokens -= 1


def _parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop ``None`` values and encode booleans the way OpenDota expects (1/0)."""
    cleaned: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        cleaned[key] = int(value) if isinstance(value, bool) else value
    return cleaned


class OpenDotaClient:
    """Async OpenDota API client. Use as ``async with OpenDotaClient() as client: ...``."""

    def __init__(
        self,
        *,
        api_key: str | None = _UNSET,
        base_url: str = BASE_URL,
        requests_per_minute: float = DEFAULT_REQUESTS_PER_MINUTE,
        burst: float = 1.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        retry_after_max: float = 120.0,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.api_key = settings.opendota_api_key if api_key is _UNSET else api_key
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.retry_after_max = retry_after_max
        self._sleep = sleep
        self._rng = rng
        self.limiter = TokenBucket(requests_per_minute, burst, clock=clock, sleep=sleep)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "dota-insights"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: a random delay in [d/2, d], d = base * 2**attempt."""
        delay = min(self.backoff_max, self.backoff_base * 2**attempt)
        return delay / 2 + delay / 2 * self._rng()

    async def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        query = _clean_params(params)
        if self.api_key:
            query["api_key"] = self.api_key
        # Error messages name the path only so the API key never ends up in logs.
        label = f"{method} {path}"

        for attempt in range(self.max_retries + 1):
            last_attempt = attempt == self.max_retries
            await self.limiter.acquire()
            try:
                response = await self._http.request(method, path, params=query)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise OpenDotaUnavailableError(f"{label} failed: {type(exc).__name__}") from exc
                await self._sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if last_attempt:
                    if status == 429:
                        raise OpenDotaRateLimitedError(
                            f"{label} rate limited by OpenDota", retry_after=retry_after
                        )
                    raise OpenDotaUnavailableError(
                        f"{label} failed with HTTP {status}", status_code=status
                    )
                if retry_after is not None:
                    await self._sleep(min(retry_after, self.retry_after_max))
                else:
                    await self._sleep(self._backoff(attempt))
                continue
            if status == 404:
                raise OpenDotaNotFoundError(f"{label} not found", status_code=status)
            if status >= 400:
                raise OpenDotaError(f"{label} failed with HTTP {status}", status_code=status)

            try:
                return response.json()
            except ValueError as exc:
                raise OpenDotaError(f"{label} returned invalid JSON", status_code=status) from exc

        raise AssertionError("unreachable")  # pragma: no cover

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params)

    # Heroes

    async def heroes(self) -> list[Hero]:
        """GET /heroes: basic hero metadata."""
        return await self._get("/heroes")

    async def hero_stats(self) -> list[dict[str, Any]]:
        """GET /heroStats: per-hero pick/win counts by rank bracket and pro scene."""
        return await self._get("/heroStats")

    async def hero_matchups(self, hero_id: int) -> list[HeroMatchup]:
        """GET /heroes/{hero_id}/matchups: results against each opposing hero."""
        return await self._get(f"/heroes/{hero_id}/matchups")

    async def hero_item_popularity(self, hero_id: int) -> dict[str, dict[str, int]]:
        """GET /heroes/{hero_id}/itemPopularity: item counts by game phase."""
        return await self._get(f"/heroes/{hero_id}/itemPopularity")

    async def benchmarks(self, hero_id: int) -> dict[str, Any]:
        """GET /benchmarks: stat percentiles for a hero."""
        return await self._get("/benchmarks", {"hero_id": hero_id})

    # Matches

    async def match(self, match_id: int) -> dict[str, Any]:
        """GET /matches/{match_id}: full match details."""
        return await self._get(f"/matches/{match_id}")

    async def request_parse(self, match_id: int) -> ParseRequest:
        """POST /request/{match_id}: queue a replay parse for the match."""
        return await self._request("POST", f"/request/{match_id}")

    async def public_matches(
        self,
        min_rank: int | None = None,
        less_than_match_id: int | None = None,
        max_rank: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /publicMatches: a page of recent public matches."""
        return await self._get(
            "/publicMatches",
            {"min_rank": min_rank, "max_rank": max_rank, "less_than_match_id": less_than_match_id},
        )

    async def pro_matches(self, less_than_match_id: int | None = None) -> list[dict[str, Any]]:
        """GET /proMatches: a page of recent professional matches."""
        return await self._get("/proMatches", {"less_than_match_id": less_than_match_id})

    async def live(self) -> list[dict[str, Any]]:
        """GET /live: currently live top games."""
        return await self._get("/live")

    # Players

    async def player(self, account_id: int) -> dict[str, Any]:
        """GET /players/{account_id}: player profile."""
        return await self._get(f"/players/{account_id}")

    async def player_matches(self, account_id: int, **filters: Any) -> list[dict[str, Any]]:
        """GET /players/{account_id}/matches with OpenDota filters.

        Filters are passed through as query params, e.g. ``limit``, ``offset``, ``hero_id``,
        ``win``, ``date``, ``lobby_type``, ``significant`` or ``project`` (a list repeats the
        param). ``None`` values are dropped and booleans are sent as 1/0.
        """
        return await self._get(f"/players/{account_id}/matches", filters)

    async def player_heroes(self, account_id: int) -> list[dict[str, Any]]:
        """GET /players/{account_id}/heroes: per-hero record for a player."""
        return await self._get(f"/players/{account_id}/heroes")

    # Misc

    async def constants(self, resource: str) -> Any:
        """GET /constants/{resource}: static game data (e.g. ``heroes``, ``items``)."""
        return await self._get(f"/constants/{resource}")

    async def explorer(self, sql: str) -> dict[str, Any]:
        """GET /explorer: run a read-only SQL query against OpenDota's database."""
        return await self._get("/explorer", {"sql": sql})
