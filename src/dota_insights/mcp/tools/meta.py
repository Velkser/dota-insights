"""Server metadata tools."""

from fastmcp import FastMCP

from dota_insights import __version__
from dota_insights.config import settings


def register(mcp: FastMCP) -> None:
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
