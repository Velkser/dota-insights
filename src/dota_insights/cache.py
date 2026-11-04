"""In-process TTL cache for expensive source calls.

Hero and item aggregates change slowly, while the free OpenDota tier allows 3,000 calls a
day, so tools cache what they fetch. Decorate an async function with ``@cached(ttl=...)``;
calls with equal arguments within ``ttl`` seconds share one result, and concurrent misses
share one in-flight call.
"""

import asyncio
import functools
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

# Suggested TTLs, in seconds, by how fast the underlying data changes.
TTL_REFERENCE = 24 * 3600  # constants, heroes, items
TTL_AGGREGATE = 6 * 3600  # hero stats, matchups, builds, benchmarks
TTL_LIVE = 60  # live matches
TTL_MATCH = 7 * 24 * 3600  # a finished match does not change (except when parsed later)

_registry: list["_Cache"] = []


class _Cache:
    def __init__(self, ttl: float, maxsize: int, clock: Callable[[], float]) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self.clock = clock
        self.entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self.inflight: dict[Any, asyncio.Future] = {}

    def clear(self) -> None:
        self.entries.clear()


def _key(args: tuple, kwargs: dict) -> Any:
    return (args, tuple(sorted(kwargs.items())))


def cached(
    ttl: float, maxsize: int = 1024, clock: Callable[[], float] = time.monotonic
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Memoize an async function for ``ttl`` seconds (LRU-bounded to ``maxsize`` entries)."""

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        cache = _Cache(ttl, maxsize, clock)
        _registry.append(cache)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = _key(args, kwargs)
            now = cache.clock()
            hit = cache.entries.get(key)
            if hit is not None and now - hit[0] < cache.ttl:
                cache.entries.move_to_end(key)
                return hit[1]
            if key in cache.inflight:
                return await asyncio.shield(cache.inflight[key])
            future = asyncio.get_running_loop().create_future()
            cache.inflight[key] = future
            try:
                value = await fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
                future.exception()  # mark retrieved so a lone caller does not warn
                raise
            else:
                future.set_result(value)
                cache.entries[key] = (cache.clock(), value)
                cache.entries.move_to_end(key)
                while len(cache.entries) > cache.maxsize:
                    cache.entries.popitem(last=False)
                return value
            finally:
                del cache.inflight[key]

        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorate


def clear_all() -> None:
    """Empty every ``@cached`` function's cache (for tests)."""
    for cache in _registry:
        cache.clear()
