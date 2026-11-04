"""Public MCP server exposing Dota 2 analytics tools."""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from dota_insights import __version__
from dota_insights.config import settings
from dota_insights.logging import ToolCallLoggingMiddleware, setup_logging
from dota_insights.mcp.tools import register_all

mcp = FastMCP(
    name="dota-insights",
    instructions=(
        "Dota 2 analytics tools. Every result includes sample size, patch and caveats; "
        "report them to the user and do not present low-sample results as facts."
    ),
)
mcp.add_middleware(ToolCallLoggingMiddleware())
register_all(mcp)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    """Liveness probe for load balancers and container orchestrators."""
    return JSONResponse({"status": "ok", "version": __version__})


def run() -> None:
    """Serve over Streamable HTTP at http://<host>:<port>/mcp."""
    setup_logging()
    mcp.run(transport="http", host=settings.host, port=settings.port)
