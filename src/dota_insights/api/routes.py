"""``/api/v1``: read-only REST endpoints, each one MCP tool, for developers who do not use MCP.

Every endpoint calls its tool through ``mcp.call_tool``, so it answers with the tool's own
envelope (status, result, sample size, filters, period, caveats, sources) and goes through the
same middleware as an MCP call: timeouts and error envelopes, logging, usage events (recorded
with ``channel: "api"``) and the rate limiter, which counts API calls against the key's quotas
(see ``dota_insights.api.context``). Source calls stay under the same server-wide caps.

Path parameters and query parameters are the tool's arguments; ``Filters`` fields (patch,
rank_bracket, position, game_mode, min_sample) are plain query parameters. Requests need
``Authorization: Bearer <key>``. Failures before the tool runs (no or bad key, quota spent,
invalid arguments, unknown path) answer with an ``error`` envelope and a 4xx/5xx status; a tool
that ran answers 200 whatever its envelope ``status``.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as ToolValidationError
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from dota_insights.api import keys
from dota_insights.api.context import ApiCaller, calling
from dota_insights.config import settings
from dota_insights.mcp.envelope import error
from dota_insights.models import Filters

PREFIX = "/api/v1"
DOCS_URL = "https://github.com/Velkser/dota-insights/blob/main/docs/api.md"
FILTER_FIELDS: tuple[str, ...] = tuple(Filters.model_fields)
NO_STORE = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class Endpoint:
    """``GET PREFIX + path`` runs ``tool``; ``{name}`` segments are tool arguments."""

    path: str
    tool: str
    summary: str
    tag: str

    @property
    def path_params(self) -> tuple[str, ...]:
        return tuple(part[1:-1] for part in self.path.split("/") if part.startswith("{"))


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("/heroes/{hero}", "get_hero_overview", "Hero overview", "heroes"),
    Endpoint("/heroes/{hero}/matchups", "get_hero_matchups", "Hero matchups", "heroes"),
    Endpoint("/heroes/{hero}/builds", "get_hero_builds", "Hero item and skill builds", "heroes"),
    Endpoint("/tier-list", "get_tier_list", "Hero tier list", "heroes"),
    Endpoint("/items/{item}", "get_item_overview", "Item overview", "items"),
    Endpoint("/matches/{match_id}", "analyze_match", "Match analysis", "matches"),
    Endpoint("/players/{player}", "get_player_profile", "Player profile", "players"),
)


def failure(
    status_code: int, message: str, hint: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    """An ``error`` envelope, as the tools return it, with an HTTP error status."""
    return JSONResponse(
        error(message, hint=hint).model_dump(mode="json"),
        status_code=status_code,
        headers=NO_STORE | (headers or {}),
    )


def _unauthorized(message: str) -> JSONResponse:
    return failure(
        401,
        message,
        f"Send Authorization: Bearer <your API key>. How to get a key: {DOCS_URL}",
        {"WWW-Authenticate": 'Bearer realm="dota-insights"'},
    )


async def authorize(request: Request) -> ApiCaller | JSONResponse:
    """The caller for the request's API key, or the error response to send instead."""
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    given = given.strip()
    if scheme.lower() != "bearer" or not given:
        return _unauthorized("Missing API key.")
    try:
        record = await keys.authenticate(given)
    except keys.ApiNotConfiguredError:
        return failure(
            503,
            "The Developer API is not enabled on this server.",
            "The server operator has to set DOTA_INSIGHTS_API_KEY_SECRET.",
        )
    except keys.KeyStoreUnavailableError:
        return failure(503, "API keys cannot be checked right now.", "Retry in a minute.")
    if not record.valid:
        return _unauthorized("Invalid or revoked API key.")
    return ApiCaller(
        key_id=record.id,
        per_minute=settings.api_key_per_minute if record.per_minute is None else record.per_minute,
        per_day=settings.api_key_per_day if record.per_day is None else record.per_day,
    )


def tool_arguments(
    endpoint: Endpoint, parameters: dict[str, Any], request: Request
) -> dict[str, Any] | JSONResponse:
    """The tool's arguments from the path and query, or a 422 for an unknown query parameter."""
    properties = parameters.get("properties", {})
    takes_filters = "filters" in properties
    arguments: dict[str, Any] = dict(request.path_params)
    filters: dict[str, Any] = {}
    unknown = []
    for name, value in request.query_params.items():
        if takes_filters and name in FILTER_FIELDS:
            filters[name] = value
        elif name in properties and name != "filters" and name not in endpoint.path_params:
            arguments[name] = value
        else:
            unknown.append(name)
    if unknown:
        allowed = [p for p in properties if p != "filters" and p not in endpoint.path_params]
        allowed += list(FILTER_FIELDS) if takes_filters else []
        return failure(
            422,
            f"Unknown query parameter(s): {', '.join(sorted(unknown))}.",
            f"This endpoint takes: {', '.join(allowed) or 'no query parameters'}.",
        )
    if filters:
        arguments["filters"] = filters
    return arguments


def _validation_message(exc: Exception) -> str:
    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return str(exc).splitlines()[0]
    parts = []
    for item in cause.errors()[:5]:
        # Filters fields are query parameters here, so "filters.patch" reads as "patch".
        loc = [str(p) for p in item["loc"] if p != "filters"]
        parts.append(f"{'.'.join(loc) or 'arguments'}: {item['msg']}")
    return "; ".join(parts)


Handler = Callable[[Request], Awaitable[JSONResponse]]


def endpoint_handler(mcp: FastMCP, endpoint: Endpoint) -> Handler:
    async def handle(request: Request) -> JSONResponse:
        caller = await authorize(request)
        if isinstance(caller, JSONResponse):
            return caller
        tool = await mcp.get_tool(endpoint.tool)
        arguments = tool_arguments(endpoint, tool.parameters, request)
        if isinstance(arguments, JSONResponse):
            return arguments
        with calling(caller):
            try:
                result = await mcp.call_tool(endpoint.tool, arguments)
            except ToolValidationError as exc:
                return failure(
                    422,
                    f"Invalid parameters: {_validation_message(exc)}",
                    f"Check the parameters against {PREFIX}/openapi.json.",
                )
            except ToolError as exc:
                return failure(500, str(exc), "Retry once; if it fails again, change the request.")
        if caller.retry_after is not None:
            return JSONResponse(
                result.structured_content,
                status_code=429,
                headers=NO_STORE | {"Retry-After": str(caller.retry_after)},
            )
        return JSONResponse(result.structured_content, headers=NO_STORE)

    handle.__name__ = f"api_{endpoint.tool}"
    return handle


def register_api_routes(mcp: FastMCP) -> None:
    from dota_insights.api.openapi import build_document

    document: dict[str, Any] = {}

    @mcp.custom_route(PREFIX, methods=["GET"], include_in_schema=False)
    async def api_index(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "version": "v1",
                "openapi": f"{PREFIX}/openapi.json",
                "docs": DOCS_URL,
                "endpoints": [
                    {"path": PREFIX + e.path, "tool": e.tool, "summary": e.summary}
                    for e in ENDPOINTS
                ],
            }
        )

    @mcp.custom_route(f"{PREFIX}/openapi.json", methods=["GET"], include_in_schema=False)
    async def api_openapi(request: Request) -> JSONResponse:
        # Built from the registered tools on first request; they do not change at runtime.
        if not document:
            document.update(await build_document(mcp))
        return JSONResponse(document, headers={"Cache-Control": "public, max-age=3600"})

    for endpoint in ENDPOINTS:
        mcp.custom_route(PREFIX + endpoint.path, methods=["GET"], include_in_schema=False)(
            endpoint_handler(mcp, endpoint)
        )

    # Registered last, so it only catches paths no endpoint above matched.
    @mcp.custom_route(PREFIX + "/{path:path}", methods=["GET"], include_in_schema=False)
    async def api_not_found(request: Request) -> JSONResponse:
        return failure(
            404,
            f"No such endpoint: {request.url.path}.",
            f"The endpoints are listed at {PREFIX} and in {PREFIX}/openapi.json.",
        )
