from fastmcp import FastMCP

from dota_insights import __version__
from dota_insights.config import settings

mcp = FastMCP(
    name="dota-insights",
    instructions=(
        "Dota 2 analytics tools. Every result includes sample size, patch and caveats; "
        "report them to the user and do not present low-sample results as facts."
    ),
)


@mcp.tool
def server_info() -> dict:
    """Return the server version and which data sources are configured."""
    return {
        "name": "dota-insights",
        "version": __version__,
        "sources": {
            "opendota": True,
            "stratz": settings.stratz_api_token is not None,
        },
        "tools_available": [],
    }


def run() -> None:
    """Serve over Streamable HTTP at http://<host>:<port>/mcp."""
    mcp.run(transport="http", host=settings.host, port=settings.port)
