import re

import httpx
import pytest

from dota_insights.mcp.asgi import app
from dota_insights.mcp.server import mcp


@pytest.fixture
async def client():
    # The pages need no lifespan; /mcp does, and the session manager runs once per app.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_landing_is_html_in_english_by_default(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-language"] == "en"
    assert "Accept-Language" in response.headers["vary"]
    assert '<html lang="en">' in response.text
    assert "https://dota-insights.vercel.app/mcp" in response.text
    assert "<script" not in response.text


@pytest.mark.parametrize(
    ("query", "accept", "expected"),
    [
        ("", "ru-RU,ru;q=0.9,en;q=0.8", "ru"),
        ("", "en-US,en;q=0.9,ru;q=0.5", "en"),
        ("", "de-DE,de;q=0.9,ru;q=0.4", "ru"),
        ("", "fr-FR", "en"),
        ("?lang=en", "ru-RU", "en"),
        ("?lang=ru", "en-US", "ru"),
        ("?lang=xx", "ru", "ru"),
    ],
)
async def test_language_selection(client, query, accept, expected):
    for path in ("/", "/privacy"):
        response = await client.get(path + query, headers={"Accept-Language": accept})
        assert response.status_code == 200
        assert response.headers["content-language"] == expected
        assert f'<html lang="{expected}">' in response.text


async def test_landing_lists_exactly_the_registered_tools(client):
    response = await client.get("/")
    listed = re.findall(r'<li id="tool-([a-z_]+)">', response.text)
    registered = {tool.name for tool in await mcp.list_tools()}
    assert len(listed) == len(set(listed))
    assert set(listed) == registered
    assert f"{len(registered)} tools" in response.text


async def test_landing_has_every_section_in_both_languages(client):
    for lang in ("en", "ru"):
        text = (await client.get(f"/?lang={lang}")).text
        for anchor in ("about", "tools", "examples", "claude-code", "claude-desktop", "cursor"):
            assert f'id="{anchor}"' in text
        assert 'id="data"' in text and 'href="https://stratz.com"' in text
        assert "mcp-remote" in text
        assert "docs/connect/claude-code.md" in text


async def test_privacy_page_is_marked_as_template(client):
    en = await client.get("/privacy?lang=en")
    ru = await client.get("/privacy?lang=ru")
    assert en.status_code == ru.status_code == 200
    assert en.headers["content-type"].startswith("text/html")
    assert "not legal advice" in en.text
    assert "не юридическая консультация" in ru.text
    for text in (en.text, ru.text):
        for third_party in ("Vercel", "Neon", "Upstash", "OpenDota", "STRATZ"):
            assert third_party in text


async def test_other_routes_still_work(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    assert (await client.get("/status")).status_code == 200
    assert (await client.post("/")).status_code == 405

    fresh = mcp.http_app(stateless_http=True, json_response=True)
    async with (
        fresh.router.lifespan_context(fresh),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=fresh), base_url="http://test") as c,
    ):
        response = await c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["tools"]
