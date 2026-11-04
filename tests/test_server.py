from fastmcp import Client

from dota_insights import __version__
from dota_insights.mcp.server import mcp


async def test_server_info():
    async with Client(mcp) as client:
        result = await client.call_tool("server_info", {})
    assert result.data["version"] == __version__
    assert result.data["name"] == "dota-insights"
