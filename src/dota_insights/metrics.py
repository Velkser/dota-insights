"""Per-call metrics, gathered while a tool runs and logged when it returns.

``ToolCallLoggingMiddleware`` opens a ``CallMetrics`` for each tool call; the cache, the source
clients and the shared quotas record into whichever one is current (a context variable, so
concurrent calls on one instance do not mix). Outside a tool call the ``record_*`` helpers do
nothing.
"""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CallMetrics:
    cache_hits: int = 0  # served from this instance's memory
    shared_cache_hits: int = 0  # served from Redis
    cache_misses: int = 0  # had to call the source
    source_calls: Counter[str] = field(default_factory=Counter)
    quota_left: dict[str, int] = field(default_factory=dict)  # source -> calls left today
    rate_limited: bool = False

    def as_log(self) -> dict[str, Any]:
        hits = self.cache_hits + self.shared_cache_hits
        return {
            "sources": sorted(self.source_calls),
            "source_calls": dict(self.source_calls),
            "cache_hits": hits,
            "shared_cache_hits": self.shared_cache_hits,
            "cache_misses": self.cache_misses,
            # Answered without any source request, thanks to the cache.
            "cache_hit": hits > 0 and not self.source_calls,
            "quota_left": dict(self.quota_left),
        }


_current: ContextVar[CallMetrics | None] = ContextVar("dota_insights_call_metrics", default=None)


@contextmanager
def track_call() -> Iterator[CallMetrics]:
    """Collect metrics for the code run inside the block."""
    metrics = CallMetrics()
    token = _current.set(metrics)
    try:
        yield metrics
    finally:
        _current.reset(token)


def current() -> CallMetrics | None:
    return _current.get()


def record_cache(outcome: str) -> None:
    """``outcome``: ``hit`` (memory), ``shared`` (Redis) or ``miss``."""
    metrics = _current.get()
    if metrics is None:
        return
    if outcome == "hit":
        metrics.cache_hits += 1
    elif outcome == "shared":
        metrics.shared_cache_hits += 1
    else:
        metrics.cache_misses += 1


def record_source_call(source: str) -> None:
    metrics = _current.get()
    if metrics is not None:
        metrics.source_calls[source] += 1


def record_quota(source: str, left: int) -> None:
    metrics = _current.get()
    if metrics is not None:
        metrics.quota_left[source] = left
