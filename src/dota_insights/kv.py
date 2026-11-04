"""Shared key-value state for serverless instances: Upstash Redis over its REST API.

Vercel instances share no memory, so the cache, the server-wide source quotas, the per-client
rate limits and the alert throttle keep their state here when Redis is configured
(``UPSTASH_REDIS_REST_URL`` / ``UPSTASH_REDIS_REST_TOKEN``, see ``config.Settings``).

Redis is an optimisation, never a dependency: every method raises ``KVError`` on any failure,
callers fall back to in-process state, and after a failure the client skips Redis for
``settings.redis_cooldown`` seconds so an outage does not add a timeout to every call.

Tools get the store through ``get_kv()`` (None when Redis is not configured); tests install a
fake with ``use_kv(...)``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

import httpx

from dota_insights.config import settings

logger = logging.getLogger(__name__)

# Increment each key and give it a TTL when it is new; one round trip and one Redis command.
INCR_WINDOWS_SCRIPT = """
local out = {}
for i, key in ipairs(KEYS) do
  local n = redis.call('INCR', key)
  if n == 1 then redis.call('EXPIRE', key, ARGV[i]) end
  out[i] = n
end
return out
"""


class KVError(Exception):
    """The shared store failed or is cooling down after a failure."""


class KV(ABC):
    """The operations the server needs from a shared store. Values are strings."""

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def mget(self, keys: Sequence[str]) -> list[str | None]: ...

    @abstractmethod
    async def set(self, key: str, value: str, *, ex: int) -> None: ...

    @abstractmethod
    async def set_nx(self, key: str, value: str, *, ex: int) -> bool:
        """Set ``key`` only if it does not exist; True if it was set."""

    @abstractmethod
    async def incr(self, key: str) -> int: ...

    @abstractmethod
    async def incr_windows(self, windows: Sequence[tuple[str, int]]) -> list[int]:
        """Increment each ``(key, ttl)`` counter, setting ``ttl`` seconds on new keys."""


class UpstashKV(KV):
    """Upstash Redis REST client (https://upstash.com/docs/redis/features/restapi)."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 1.5,
        cooldown: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.monotonic,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.cooldown = cooldown
        self._transport = transport
        self._clock = clock
        self._down_until = 0.0
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def available(self) -> bool:
        """False while cooling down after a failure."""
        return self._clock() >= self._down_until

    def _client(self) -> httpx.AsyncClient:
        # A connection pool belongs to the event loop that opened it.
        loop = asyncio.get_running_loop()
        if self._http is None or self._loop is not loop:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            self._loop = loop
        return self._http

    async def command(self, *args: Any) -> Any:
        if not self.available:
            raise KVError("Redis is cooling down after a failure")
        try:
            response = await self._client().post(self.url, json=[str(a) for a in args])
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._fail(type(exc).__name__)
            raise KVError(f"Redis request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            # Only the status goes to the log: the error text could echo the command.
            self._fail(f"HTTP {response.status_code}")
            raise KVError(f"Redis answered HTTP {response.status_code}")
        return payload["result"]

    def _fail(self, reason: str) -> None:
        if self.available:
            logger.warning("redis unavailable, using in-memory state", extra={"error": reason})
        self._down_until = self._clock() + self.cooldown

    async def get(self, key: str) -> str | None:
        return await self.command("GET", key)

    async def mget(self, keys: Sequence[str]) -> list[str | None]:
        if not keys:
            return []
        return await self.command("MGET", *keys)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        await self.command("SET", key, value, "EX", max(1, int(ex)))

    async def set_nx(self, key: str, value: str, *, ex: int) -> bool:
        return await self.command("SET", key, value, "NX", "EX", max(1, int(ex))) == "OK"

    async def incr(self, key: str) -> int:
        return int(await self.command("INCR", key))

    async def incr_windows(self, windows: Sequence[tuple[str, int]]) -> list[int]:
        keys = [key for key, _ in windows]
        ttls = [max(1, int(ttl)) for _, ttl in windows]
        result = await self.command("EVAL", INCR_WINDOWS_SCRIPT, len(keys), *keys, *ttls)
        return [int(n) for n in result]

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


_UNSET: Any = object()
_kv: KV | None | Any = _UNSET


def get_kv() -> KV | None:
    """The shared store, or None when Redis is not configured."""
    global _kv
    if _kv is _UNSET:
        if settings.redis_rest_url and settings.redis_rest_token:
            _kv = UpstashKV(
                settings.redis_rest_url,
                settings.redis_rest_token,
                timeout=settings.redis_timeout,
                cooldown=settings.redis_cooldown,
            )
        else:
            _kv = None
    return _kv


def use_kv(kv: KV | None) -> None:
    """Replace the shared store (for tests); ``None`` means no Redis."""
    global _kv
    _kv = kv


def reset_kv() -> None:
    """Forget the store so the next ``get_kv()`` reads the settings again."""
    global _kv
    _kv = _UNSET


def backend() -> str:
    """``redis`` when a shared store is configured and not cooling down, else ``memory``."""
    kv = get_kv()
    if kv is None or (isinstance(kv, UpstashKV) and not kv.available):
        return "memory"
    return "redis"
