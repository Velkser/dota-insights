"""Public MCP server exposing Dota 2 analytics tools."""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from dota_insights import __version__
from dota_insights.config import settings
from dota_insights.logging import ToolCallLoggingMiddleware, setup_logging
from dota_insights.mcp import site
from dota_insights.mcp.admin import register_admin_routes
from dota_insights.mcp.errors import ErrorEnvelopeMiddleware
from dota_insights.mcp.prompts import register_prompts
from dota_insights.mcp.ratelimit import RateLimitMiddleware
from dota_insights.mcp.status import status_payload
from dota_insights.mcp.tools import register_all

mcp = FastMCP(
    name="dota-insights",
    instructions=(
        "Dota 2 analytics tools. Every result includes sample size, patch and caveats; "
        "report them to the user and do not present low-sample results as facts."
    ),
)
# Logging wraps the rate limiter and the error middleware, so it records the envelope the
# client receives (rate-limit refusals included).
mcp.add_middleware(ToolCallLoggingMiddleware())
mcp.add_middleware(RateLimitMiddleware())
mcp.add_middleware(ErrorEnvelopeMiddleware())
register_all(mcp)
register_prompts(mcp)
register_admin_routes(mcp)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    """Liveness probe for load balancers, containers and uptime checks (no I/O)."""
    return JSONResponse({"status": "ok", "version": __version__})


@mcp.custom_route("/status", methods=["GET"], include_in_schema=False)
async def status(request: Request) -> JSONResponse:
    """Source quota usage, shared-state backend and data freshness."""
    return JSONResponse(await status_payload(), headers={"Cache-Control": "no-store"})


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def landing(request: Request) -> HTMLResponse:
    """Landing page (English or Russian): what the server does, its tools, how to connect."""
    return await site.landing(request, prompts=len(await mcp.list_prompts()))


@mcp.custom_route("/privacy", methods=["GET"], include_in_schema=False)
async def privacy(request: Request) -> HTMLResponse:
    """Privacy policy (English or Russian)."""
    return await site.privacy(request)


def run() -> None:
    """Serve over Streamable HTTP at http://<host>:<port>/mcp."""
    setup_logging()
    mcp.run(transport="http", host=settings.host, port=settings.port)
