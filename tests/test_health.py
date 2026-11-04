import httpx2

from dota_insights import __version__
from dota_insights.mcp.server import mcp


async def test_health_route():
    transport = httpx2.ASGITransport(app=mcp.http_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
