import httpx

from dota_insights.config import Settings
from dota_insights.mcp.asgi import app


async def test_serverless_app_serves_health_and_stateless_mcp():
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        health = await client.get("/health")
        assert health.json()["status"] == "ok"

        # No initialize handshake or session id: each request stands alone.
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {"get_hero_overview", "server_info"} <= names


def test_database_url_falls_back_to_vercel_variable(monkeypatch):
    monkeypatch.delenv("DOTA_INSIGHTS_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@neon/db?sslmode=require")
    assert Settings(_env_file=None).database_url == "postgresql://u@neon/db?sslmode=require"

    monkeypatch.setenv("DOTA_INSIGHTS_DATABASE_URL", "postgresql://u@own/db")
    assert Settings(_env_file=None).database_url == "postgresql://u@own/db"
