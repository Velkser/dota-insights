"""TTL cache for expensive source calls: per instance, plus Redis shared by all instances.

Hero and item aggregates change slowly, while the free OpenDota tier allows 3,000 calls a
day, so tools cache what they fetch. Decorate an async function with ``@cached(ttl=...)``;
calls with equal arguments within ``ttl`` seconds share one result, and concurrent misses
share one in-flight call.

When Redis is configured (``dota_insights.kv``) a miss in this instance's memory is looked up
there before calling the source, and fresh results are written back with the same TTL, keyed
by function and a hash of the arguments. Only values that survive a JSON round trip unchanged
are shared (tuples, dataclasses or int-keyed dicts stay in memory), and so are only calls whose
arguments encode as JSON. Keys carry a generation that changes when a new patch appears
(``invalidate_shared``, called by the collector, or a deploy with newer reference data), so
patch-dependent results are not served across a patch. Redis failures never fail a call: the
cache then behaves as a plain in-memory one.
"""

import asyncio
import contextlib
import dataclasses
import enum
import functools
import hashlib
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from dota_insights import metrics
from dota_insights.config import settings
from dota_insights.kv import KVError, get_kv

logger = logging.getLogger(__name__)

# Suggested TTLs, in seconds, by how fast the underlying data changes.
TTL_REFERENCE = 24 * 3600  # constants, heroes, items
TTL_AGGREGATE = 6 * 3600  # hero stats, matchups, builds, benchmarks
TTL_LIVE = 60  # live matches
TTL_MATCH = 7 * 24 * 3600  # a finished match does not change (except when parsed later)

GENERATION_KEY = "di:cache:gen"
# How long an instance trusts its copy of the generation before reading it again.
GENERATION_REFRESH = 60.0

_registry: list["_Cache"] = []
_generation: tuple[float, str] | None = None  # (read at, value)


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


def _encode_arg(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, set | frozenset):
        return sorted(value, key=repr)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"not shareable: {type(value).__name__}")


def _shared_key(name: str, generation: str, args: tuple, kwargs: dict) -> str | None:
    try:
        encoded = json.dumps([args, kwargs], sort_keys=True, default=_encode_arg)
    except (TypeError, ValueError):
        return None
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:32]
    return f"di:cache:{generation}:{name}:{digest}"


def _shareable(value: Any) -> str | None:
    """``value`` as JSON if it comes back equal and fits the size cap, else None."""
    try:
        text = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(text) > settings.cache_shared_max_bytes or json.loads(text) != value:
        return None
    return text


def _reference_patch() -> str:
    from dota_insights.reference import patches

    known = patches()
    return known[-1].name if known else "none"


async def cache_generation() -> str | None:
    """The current shared-cache generation, or None when Redis is unavailable."""
    global _generation
    kv = get_kv()
    if kv is None:
        return None
    now = time.monotonic()
    if _generation is not None and now - _generation[0] < GENERATION_REFRESH:
        return _generation[1]
    try:
        counter = await kv.get(GENERATION_KEY) or "0"
    except KVError:
        return None
    _generation = (now, f"{_reference_patch()}.{counter}")
    return _generation[1]


async def invalidate_shared(reason: str) -> bool:
    """Start a new shared-cache generation (e.g. a new patch); False if Redis is unavailable.

    Other instances pick it up within ``GENERATION_REFRESH`` seconds; old keys expire by TTL.
    """
    global _generation
    kv = get_kv()
    if kv is None:
        return False
    try:
        counter = await kv.incr(GENERATION_KEY)
    except KVError:
        return False
    _generation = None
    logger.info("shared cache invalidated", extra={"reason": reason, "generation": counter})
    return True


async def _shared_get(key: str) -> tuple[bool, Any]:
    kv = get_kv()
    if kv is None:
        return False, None
    try:
        text = await kv.get(key)
    except KVError:
        return False, None
    if text is None:
        return False, None
    try:
        return True, json.loads(text)
    except ValueError:
        return False, None


async def _shared_set(key: str, value: Any, ttl: float) -> None:
    kv = get_kv()
    text = _shareable(value) if kv is not None else None
    if kv is None or text is None:
        return
    with contextlib.suppress(KVError):
        await kv.set(key, text, ex=math.ceil(ttl))


def cached(
    ttl: float,
    maxsize: int = 1024,
    clock: Callable[[], float] = time.monotonic,
    *,
    shared: bool = True,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Memoize an async function for ``ttl`` seconds (LRU-bounded to ``maxsize`` entries).

    ``shared=False`` keeps results in this instance's memory only.
    """

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        cache = _Cache(ttl, maxsize, clock)
        _registry.append(cache)
        name = f"{fn.__module__}.{fn.__qualname__}"

        def remember(key: Any, value: Any) -> None:
            cache.entries[key] = (cache.clock(), value)
            cache.entries.move_to_end(key)
            while len(cache.entries) > cache.maxsize:
                cache.entries.popitem(last=False)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = _key(args, kwargs)
            now = cache.clock()
            hit = cache.entries.get(key)
            if hit is not None and now - hit[0] < cache.ttl:
                cache.entries.move_to_end(key)
                metrics.record_cache("hit")
                return hit[1]
            if key in cache.inflight:
                metrics.record_cache("hit")
                return await asyncio.shield(cache.inflight[key])
            future = asyncio.get_running_loop().create_future()
            cache.inflight[key] = future
            try:
                generation = await cache_generation() if shared else None
                shared_key = (
                    _shared_key(name, generation, args, kwargs) if generation is not None else None
                )
                found, value = await _shared_get(shared_key) if shared_key else (False, None)
                if found:
                    metrics.record_cache("shared")
                else:
                    metrics.record_cache("miss")
                    value = await fn(*args, **kwargs)
                    if shared_key:
                        await _shared_set(shared_key, value, cache.ttl)
            except BaseException as exc:
                future.set_exception(exc)
                future.exception()  # mark retrieved so a lone caller does not warn
                raise
            else:
                future.set_result(value)
                remember(key, value)
                return value
            finally:
                del cache.inflight[key]

        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorate


def clear_all() -> None:
    """Empty every ``@cached`` function's in-memory cache (for tests)."""
    global _generation
    _generation = None
    for cache in _registry:
        cache.clear()
