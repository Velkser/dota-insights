"""Async client for the STRATZ GraphQL API (https://api.stratz.com/graphql).

A thin transport: callers write their own GraphQL documents and get the ``data`` object
back. Like the OpenDota client it applies a token-bucket rate limit and retries transient
failures (HTTP 429, 5xx and connection errors) with exponential backoff and jitter.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Self

import httpx

from dota_insights import metrics
from dota_insights.config import settings
from dota_insights.sources.opendota import Clock, Sleep, TokenBucket, _parse_retry_after
from dota_insights.sources.quota import QuotaExhaustedError, SharedQuota

BASE_URL = "https://api.stratz.com/graphql"

# Default token: 20/s, 250/min, 2,000/h, 10,000/day. Stay under the per-minute cap; the hourly
# and daily caps are left to callers' caching.
DEFAULT_REQUESTS_PER_MINUTE = 200.0

_UNSET: Any = object()


class StratzError(Exception):
    """A STRATZ request failed. ``status_code`` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StratzAuthError(StratzError):
    """The token is missing, invalid or not allowed to run the query (HTTP 401/403)."""


class StratzRateLimitedError(StratzError):
    """STRATZ kept answering 429 after all retries."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class StratzUnavailableError(StratzError):
    """STRATZ could not be reached or kept failing with 5xx."""


class StratzQueryError(StratzError):
    """The query reached STRATZ but came back with GraphQL ``errors``.

    ``data`` keeps whatever fields did resolve, so callers batching aliased fields can use them.
    """

    def __init__(self, message: str, *, errors: list[dict[str, Any]], data: Any = None) -> None:
        super().__init__(message, status_code=200)
        self.errors = errors
        self.data = data


class StratzClient:
    """Async STRATZ client. Use as ``async with StratzClient() as client: ...``."""

    def __init__(
        self,
        *,
        token: str | None = _UNSET,
        base_url: str = BASE_URL,
        requests_per_minute: float = DEFAULT_REQUESTS_PER_MINUTE,
        burst: float = 5.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        retry_after_max: float = 120.0,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        quota: SharedQuota | None = None,
    ) -> None:
        self.token = settings.stratz_api_token if token is _UNSET else token
        self.url = base_url
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.retry_after_max = retry_after_max
        self._sleep = sleep
        self._rng = rng
        self.limiter = TokenBucket(requests_per_minute, burst, clock=clock, sleep=sleep)
        self.quota = quota
        # STRATZ rejects requests without this exact User-Agent.
        headers = {"Content-Type": "application/json", "User-Agent": "STRATZ_API"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, headers=headers)

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
        delay = min(self.backoff_max, self.backoff_base * 2**attempt)
        return delay / 2 + delay / 2 * self._rng()

    async def query(self, document: str, variables: Mapping[str, Any] | None = None) -> Any:
        """Run a GraphQL ``document`` and return its ``data``. Raises on HTTP or GraphQL errors."""
        if not self.token:
            raise StratzAuthError("STRATZ token is not configured (DOTA_INSIGHTS_STRATZ_API_TOKEN)")
        payload = {"query": document, "variables": dict(variables or {})}

        for attempt in range(self.max_retries + 1):
            last_attempt = attempt == self.max_retries
            await self.limiter.acquire()
            if self.quota is not None:
                try:
                    await self.quota.acquire()
                except QuotaExhaustedError as exc:
                    raise StratzRateLimitedError(
                        "the server's daily STRATZ quota is spent", retry_after=exc.retry_after
                    ) from exc
            metrics.record_source_call("stratz")
            try:
                response = await self._http.post(self.url, json=payload)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise StratzUnavailableError(f"STRATZ failed: {type(exc).__name__}") from exc
                await self._sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if last_attempt:
                    if status == 429:
                        raise StratzRateLimitedError(
                            "STRATZ rate limited the request", retry_after=retry_after
                        )
                    raise StratzUnavailableError(
                        f"STRATZ failed with HTTP {status}", status_code=status
                    )
                if retry_after is not None:
                    await self._sleep(min(retry_after, self.retry_after_max))
                else:
                    await self._sleep(self._backoff(attempt))
                continue
            if status in (401, 403):
                raise StratzAuthError(
                    f"STRATZ refused the token (HTTP {status})", status_code=status
                )
            if status >= 400:
                raise StratzError(f"STRATZ failed with HTTP {status}", status_code=status)

            try:
                body = response.json()
            except ValueError as exc:
                raise StratzError("STRATZ returned invalid JSON", status_code=status) from exc
            if body.get("errors"):
                messages = "; ".join(str(e.get("message", e)) for e in body["errors"])
                raise StratzQueryError(
                    f"STRATZ query failed: {messages}", errors=body["errors"], data=body.get("data")
                )
            return body.get("data")

        raise AssertionError("unreachable")  # pragma: no cover
