"""Turn source failures and timeouts into ``error`` envelopes, for every tool at once.

Tools may catch source errors themselves to add context, but they do not have to:
``ErrorEnvelopeMiddleware`` catches whatever escapes a tool (OpenDota or STRATZ rate limits,
auth failures, outages, rejected queries, timeouts, unexpected bugs) and answers with an
``error(...)`` envelope whose ``hint`` says whether and when to retry. It also caps each call's
running time: ``DEFAULT_TOOL_TIMEOUT`` seconds, or ``TOOL_TIMEOUTS[name]`` for tools that fan
out to many source requests. New tools get all of this without any registration step.
"""

import asyncio
import logging
from typing import Any

import httpx
from fastmcp.exceptions import FastMCPError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult as McpToolResult
from mcp.shared.exceptions import MCPError

from dota_insights.mcp.envelope import Source, ToolResult, error
from dota_insights.models.filters import Filters
from dota_insights.sources.opendota import (
    OpenDotaError,
    OpenDotaNotFoundError,
    OpenDotaRateLimitedError,
    OpenDotaUnavailableError,
)
from dota_insights.sources.stratz import (
    StratzAuthError,
    StratzError,
    StratzQueryError,
    StratzRateLimitedError,
    StratzUnavailableError,
)

logger = logging.getLogger(__name__)

# Seconds a tool call may run before it is cancelled and answered with a timeout error.
DEFAULT_TOOL_TIMEOUT = 60.0
HEAVY_TOOL_TIMEOUT = 150.0

# Tools that aggregate over every hero, many matches or several patches. A cold cache can
# need dozens of rate-limited requests or slow OpenDota explorer queries, so they get longer.
TOOL_TIMEOUTS: dict[str, float] = dict.fromkeys(
    (
        "get_item_overview",
        "analyze_item",
        "compare_items",
        "get_item_payback",
        "get_tier_list",
        "find_hidden_gems",
        "find_hero_combos",
        "find_meta_trends",
        "get_hero_patch_history",
        "get_patch_summary",
        "get_gold_xp_curves",
        "get_neutral_items",
        "get_records",
        "get_player_hero_pool",
        "get_player_trends",
        "find_player_weaknesses",
        "compare_players",
        "get_team_stats",
        "get_head_to_head",
        "get_tournament_stats",
        "get_pro_player",
        "get_pro_builds",
    ),
    HEAVY_TOOL_TIMEOUT,
)

_NARROW = (
    "narrow the question (one hero, one patch, fewer matches or players) so it needs fewer "
    "source requests"
)


# Hint given to error envelopes a tool returned without one.
FALLBACK_HINT = (
    "Check the arguments against the tool's parameter descriptions; if they look right, retry "
    "in a minute."
)

SOURCE_RETRY_HINT = "Retry in a minute; if it keeps failing, try different arguments."


def tool_timeout(name: str) -> float:
    """Running-time limit, in seconds, for the tool called ``name``."""
    return TOOL_TIMEOUTS.get(name, DEFAULT_TOOL_TIMEOUT)


def _rate_limit_hint(retry_after: float | None) -> str:
    if retry_after and retry_after > 3600:
        # The server's daily source quota is spent; it resets at midnight UTC.
        return (
            f"Retry in about {round(retry_after / 3600)} hours ({round(retry_after)} seconds, "
            "at midnight UTC); cached answers for questions asked before still work."
        )
    wait = f"about {max(1, round(retry_after))} seconds" if retry_after else "a minute"
    return f"Retry in {wait}; if it repeats, {_NARROW}."


def map_exception(
    exc: BaseException,
    *,
    filters: Filters | None = None,
    source: list[Source] | None = None,
    timeout: float | None = None,
) -> ToolResult | None:
    """The ``error`` envelope for a source failure or timeout, or None if ``exc`` is neither.

    ``source`` overrides the source inferred from the exception.
    """
    message: str
    hint: str
    inferred: list[Source]
    match exc:
        case OpenDotaRateLimitedError():
            inferred = ["opendota"]
            message = "OpenDota rate limit reached (free tier: 60 requests a minute, 3,000 a day)."
            hint = _rate_limit_hint(exc.retry_after)
        case OpenDotaNotFoundError():
            inferred = ["opendota"]
            message = f"OpenDota has no such record: {exc}."
            hint = (
                "Check the id. New matches appear about 5 minutes after they end; private "
                "profiles show no matches. Retrying the same id will not help."
            )
        case OpenDotaUnavailableError():
            inferred = ["opendota"]
            message = f"OpenDota is unavailable right now: {exc}."
            hint = "Retry in a few minutes; the outage is on OpenDota's side."
        case OpenDotaError():
            inferred = ["opendota"]
            message = f"OpenDota request failed: {exc}."
            hint = SOURCE_RETRY_HINT
        case StratzAuthError():
            inferred = ["stratz"]
            message = "STRATZ refused the request: the server's API token is missing or invalid."
            hint = (
                "Retrying will not help until the server operator sets "
                "DOTA_INSIGHTS_STRATZ_API_TOKEN. Tools backed by OpenDota still work."
            )
        case StratzRateLimitedError():
            inferred = ["stratz"]
            message = "STRATZ rate limit reached."
            hint = _rate_limit_hint(exc.retry_after)
        case StratzUnavailableError():
            inferred = ["stratz"]
            message = f"STRATZ is unavailable right now: {exc}."
            hint = "Retry in a few minutes; the outage is on STRATZ's side."
        case StratzQueryError():
            inferred = ["stratz"]
            message = f"STRATZ rejected the query: {exc}."
            hint = (
                "Retrying the same call will fail again; change the filters (patch, rank "
                "bracket, position) or ask a simpler question."
            )
        case StratzError():
            inferred = ["stratz"]
            message = f"STRATZ request failed: {exc}."
            hint = SOURCE_RETRY_HINT
        case TimeoutError() | httpx.TimeoutException():
            inferred = []
            limit = f" after {timeout:g} seconds" if timeout else ""
            message = f"The tool timed out{limit} waiting for its data sources."
            hint = f"Retry once (data fetched so far is cached), or {_NARROW}."
        case httpx.TransportError():
            inferred = []
            message = f"A data source could not be reached ({type(exc).__name__})."
            hint = "Retry in a few minutes."
        case MCPError() if "timed out" in str(exc):
            return map_exception(TimeoutError(), filters=filters, source=source)
        case _:
            return None
    return error(message, hint=hint, filters=filters, source=source or inferred)


def source_error(
    exc: BaseException, *, filters: Filters | None = None, source: list[Source] | None = None
) -> ToolResult:
    """The envelope a tool returns after catching ``exc`` from a source client.

    Same message and retry hint the middleware gives for an uncaught one, so tools that catch
    source errors (to keep their filters in the envelope) stay consistent with those that don't.
    """
    mapped = map_exception(exc, filters=filters, source=source)
    if mapped is not None:
        return mapped
    return error(
        f"Data source failed: {exc}", hint=SOURCE_RETRY_HINT, filters=filters, source=source
    )


def retry_hint(exc: BaseException) -> str:
    """Just the retry hint for ``exc``, for tools that word the message themselves."""
    return source_error(exc).hint or SOURCE_RETRY_HINT


def _cause(exc: BaseException) -> BaseException:
    # FastMCP re-raises a failing tool's exception as ToolError(...) from the original.
    if isinstance(exc, ToolError) and exc.__cause__ is not None:
        return exc.__cause__
    return exc


async def _returns_envelope(context: MiddlewareContext) -> bool:
    server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
    if server is None:
        return False
    try:
        tool = await server.get_tool(context.message.name)
    except Exception:
        return False
    schema = (tool.output_schema or {}) if tool else {}
    return {"status", "result", "hint"} <= set(schema.get("properties", {}))


def _ensure_hint(result: Any) -> Any:
    body = getattr(result, "structured_content", None)
    if isinstance(body, dict) and body.get("status") == "error" and not body.get("hint"):
        result.structured_content = {**body, "hint": FALLBACK_HINT}
        result.content = McpToolResult(structured_content=result.structured_content).content
    return result


class ErrorEnvelopeMiddleware(Middleware):
    """Enforce per-tool timeouts and answer source failures with ``error`` envelopes.

    Error envelopes a tool returns without a hint get ``FALLBACK_HINT``.

    Argument validation errors, unknown tools and ``ToolError`` raised on purpose pass through
    unchanged. An unexpected exception becomes a generic error envelope that names only its
    type, so internal details (URLs, tokens) never reach the client.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        name = context.message.name
        limit = tool_timeout(name)
        try:
            async with asyncio.timeout(limit):
                return _ensure_hint(await call_next(context))
        except Exception as exc:
            cause = _cause(exc)
            if cause is exc and isinstance(exc, FastMCPError):
                raise
            if not await _returns_envelope(context):
                raise
            envelope = map_exception(cause, timeout=limit)
            if envelope is None:
                logger.error(
                    "unexpected tool failure",
                    extra={"tool": name, "error": type(cause).__name__},
                    exc_info=cause,
                )
                envelope = error(
                    f"The tool failed with an internal error ({type(cause).__name__}).",
                    hint="Retry once; if it fails again, rephrase the question or try "
                    "different arguments.",
                )
            return McpToolResult(structured_content=envelope.model_dump(mode="json"))
