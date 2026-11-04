"""Per-client tool-call limits, so one caller cannot spend the server's shared source quotas.

Clients are told apart by IP: Vercel sets ``x-real-ip`` and ``x-forwarded-for`` to the caller's
address (overwriting what the client sent), and other proxies usually do too. Each client gets
fixed per-minute and per-day windows for all tools, and lower ones for ``HEAVY_TOOLS``, which can
spend dozens of source requests or a slow explorer query per call (heavy calls count against both),
and lower still for ``FEEDBACK_TOOLS``, whose reports are stored for review.
Counters live in Redis when configured, else in this instance's memory. A call over a limit gets
an ``error`` envelope whose hint says how many seconds until the window resets.

Calls without an HTTP request (stdio, in-process tests) are not limited. Limits are set in
``config.Settings`` (``rate_limit_*``); ``DOTA_INSIGHTS_RATE_LIMIT_ENABLED=false`` turns them off.

Calls from the Developer API (``dota_insights.api``) are counted per API key instead of per IP,
against the key's own per-minute and per-day quotas (plus the heavy and feedback limits). They
are always enforced, whatever ``rate_limit_enabled`` says, and a refused call records the wait in
the caller's ``retry_after`` so the route can answer 429.
"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult as McpToolResult

from dota_insights import metrics
from dota_insights.api import context as api_context
from dota_insights.api.context import ApiCaller
from dota_insights.config import settings
from dota_insights.kv import KVError, get_kv
from dota_insights.mcp.envelope import error
from dota_insights.mcp.errors import TOOL_TIMEOUTS, _returns_envelope

MINUTE = 60
DAY = 86400

# Explorer-backed, multi-hero or multi-match tools: the slow-timeout tools plus the analysis
# tools that run OpenDota explorer SQL, and live win prediction (fetches and scores live games).
HEAVY_TOOLS: frozenset[str] = frozenset(TOOL_TIMEOUTS) | {
    "explore_query",
    "test_hypothesis",
    "find_matches",
    "analyze_correlation",
    "predict_live_win",
    "get_farming_stats",
    "get_farm_distribution",
    "get_comeback_stats",
    "get_objective_impact",
}

# Client reports (stored for review): limited separately and much lower.
FEEDBACK_TOOLS: frozenset[str] = frozenset({"report_wrong_answer", "suggest_feature"})


@dataclass(frozen=True)
class Limit:
    scope: str  # "all", "heavy" or "feedback"
    window: int  # seconds
    max_calls: int

    def describe(self) -> str:
        kind = {"heavy": "heavy tool calls", "feedback": "reports"}.get(self.scope, "tool calls")
        per = "a minute" if self.window == MINUTE else "a day"
        return f"{self.max_calls} {kind} {per}"


def limits_for(tool: str, caller: ApiCaller | None = None) -> list[Limit]:
    """Limits for one call of ``tool``: the client's, or the API key's for an API call."""
    per_minute, per_day = (
        (caller.per_minute, caller.per_day)
        if caller is not None
        else (settings.rate_limit_per_minute, settings.rate_limit_per_day)
    )
    limits = [Limit("all", MINUTE, per_minute), Limit("all", DAY, per_day)]
    if tool in HEAVY_TOOLS:
        limits += [
            Limit("heavy", MINUTE, settings.rate_limit_heavy_per_minute),
            Limit("heavy", DAY, settings.rate_limit_heavy_per_day),
        ]
    if tool in FEEDBACK_TOOLS:
        limits += [
            Limit("feedback", MINUTE, settings.rate_limit_feedback_per_minute),
            Limit("feedback", DAY, settings.rate_limit_feedback_per_day),
        ]
    return [limit for limit in limits if limit.max_calls > 0]


def client_ip() -> str | None:
    """The caller's IP from the current HTTP request, or None outside one."""
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    real = request.headers.get("x-real-ip")
    if real and real.strip():
        return real.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and forwarded.split(",")[0].strip():
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def client_id(ip: str) -> str:
    """A short hash of the IP, so keys and logs do not hold raw addresses."""
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


class RateLimiter:
    """Fixed-window counters per client and limit."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._local: dict[str, int] = {}

    async def _count(self, windows: list[tuple[str, int]]) -> list[int]:
        kv = get_kv()
        if kv is not None:
            try:
                return await kv.incr_windows(windows)
            except KVError:
                pass
        current = {key for key, _ in windows}
        # Without Redis, forget windows that have passed (keys carry the window index).
        if len(self._local) > 10_000:
            self._local = {k: v for k, v in self._local.items() if k in current}
        for key, _ in windows:
            self._local[key] = self._local.get(key, 0) + 1
        return [self._local[key] for key, _ in windows]

    async def hit(
        self, client: str, tool: str, caller: ApiCaller | None = None
    ) -> tuple[Limit, float] | None:
        """Count a call; the exceeded limit and seconds until it resets, or None if allowed."""
        limits = limits_for(tool, caller)
        if not limits:
            return None
        now = self._clock()
        windows = [
            (f"di:rl:{client}:{lim.scope}:{lim.window}:{int(now // lim.window)}", 2 * lim.window)
            for lim in limits
        ]
        counts = await self._count(windows)
        exceeded = [
            (lim, lim.window - now % lim.window)
            for lim, count in zip(limits, counts, strict=True)
            if count > lim.max_calls
        ]
        return max(exceeded, key=lambda pair: pair[1]) if exceeded else None


class RateLimitMiddleware(Middleware):
    """Answer calls over a client's limits with an ``error`` envelope instead of running them."""

    def __init__(self, limiter: RateLimiter | None = None) -> None:
        self.limiter = limiter or RateLimiter()

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        caller = api_context.current()
        if caller is not None:
            client = f"key-{caller.key_id}"
        else:
            if not settings.rate_limit_enabled:
                return await call_next(context)
            ip = client_ip()
            if ip is None:
                return await call_next(context)
            client = client_id(ip)
        tool = context.message.name
        over = await self.limiter.hit(client, tool, caller)
        if over is None:
            return await call_next(context)
        limit, reset = over
        seconds = max(1, round(reset))
        call = metrics.current()
        if call is not None:
            call.rate_limited = True
        if caller is not None:
            caller.retry_after = seconds
        who = "your API key" if caller is not None else "your address"
        message = f"Rate limit reached for {who}: at most {limit.describe()}."
        hint = f"Retry in about {seconds} seconds, when the limit resets." + (
            " This tool is one of the heavy ones (explorer queries, multi-hero or "
            "multi-match aggregates); lighter tools have a higher limit."
            if limit.scope == "heavy"
            else ""
        )
        if not await _returns_envelope(context):
            raise ToolError(f"{message} {hint}")
        envelope = error(message, hint=hint)
        return McpToolResult(structured_content=envelope.model_dump(mode="json"))
