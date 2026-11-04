"""Which API key the current tool call is made for, if it comes from the REST API.

The REST routes call tools through the MCP server (``mcp.call_tool``), so the middleware stack
runs as for an MCP client. ``RateLimitMiddleware`` reads the caller set here to count the call
against the key's quotas instead of the client's IP, and stores how long until a spent quota
resets in ``retry_after``, which the route turns into a 429.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ApiCaller:
    key_id: str
    per_minute: int
    per_day: int
    # Seconds until the exceeded quota resets, set by the rate limiter when it refused the call.
    retry_after: int | None = None


_caller: ContextVar[ApiCaller | None] = ContextVar("api_caller", default=None)


def current() -> ApiCaller | None:
    """The API key the current call is made for, or None for an MCP call."""
    return _caller.get()


@contextmanager
def calling(caller: ApiCaller) -> Iterator[ApiCaller]:
    token = _caller.set(caller)
    try:
        yield caller
    finally:
        _caller.reset(token)
