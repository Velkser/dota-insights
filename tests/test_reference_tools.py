import json

import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError

from dota_insights.mcp.server import mcp
from dota_insights.mcp.tools.reference import METRIC_DEFINITIONS
from dota_insights.reference import heroes, items, patches


async def lookup(query: str, kind: str = "any") -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool("lookup_reference", {"query": query, "kind": kind})
    return result.structured_content


async def read(uri: str):
    async with Client(mcp) as client:
        contents = await client.read_resource(uri)
    return json.loads(contents[0].text)


@pytest.mark.parametrize(
    ("query", "kind", "expected_kind", "name"),
    [
        ("Фантомка", "any", "hero", "Phantom Assassin"),
        ("PA", "hero", "hero", "Phantom Assassin"),
        ("bkb", "any", "item", "Black King Bar"),
        ("фь", "hero", "hero", "Anti-Mage"),  # "am" typed in the Russian layout
        ("Black Hole", "any", "ability", "Black Hole"),
        ("mana void", "ability", "ability", "Mana Void"),
    ],
)
async def test_lookup_resolves(query, kind, expected_kind, name):
    data = await lookup(query, kind)
    assert data["status"] == "ok"
    assert data["result"]["kind"] == expected_kind
    assert data["result"]["record"]["name"] == name
    assert data["result"]["data_patch"] == patches()[-1].name
    assert data["source"] == ["opendota"]


async def test_hero_record_fields():
    record = (await lookup("Anti-Mage", "hero"))["result"]["record"]
    assert record["id"] == 1
    assert record["primary_attribute"] == "Agility"
    assert "Carry" in record["roles"]
    assert {"name": "Blink", "internal_name": "antimage_blink"} in record["abilities"]
    assert record["talents"] and all(t["level"] for t in record["talents"])


async def test_item_record_fields():
    record = (await lookup("bkb", "item"))["result"]["record"]
    assert record["internal_name"] == "black_king_bar"
    assert record["cost"] > 0
    assert any(c["internal_name"] == "ogre_axe" for c in record["components"])


async def test_ability_record_names_owner():
    record = (await lookup("Black Hole", "ability"))["result"]["record"]
    assert record["hero"] == "Enigma"
    assert record["internal_name"] == "enigma_black_hole"


async def test_ambiguous_lookup_lists_candidates():
    data = await lookup("blink")
    assert data["status"] == "error"
    assert "Blink Dagger" in data["message"]
    assert "Anti-Mage" in data["message"]


async def test_unknown_lookup_is_error():
    data = await lookup("zzqqxx")
    assert data["status"] == "error"


async def test_resources_are_listed():
    async with Client(mcp) as client:
        resources = {str(r.uri) for r in await client.list_resources()}
        templates = {t.uri_template for t in await client.list_resource_templates()}
    assert {
        "heroes://list",
        "items://list",
        "patches://list",
        "metrics://definitions",
    } <= resources
    assert {"heroes://{hero}", "items://{item}"} <= templates


async def test_list_resources_content():
    hero_list = await read("heroes://list")
    assert len(hero_list) == len(heroes())
    assert hero_list[0]["name"] == "Anti-Mage"

    item_list = await read("items://list")
    assert item_list and not any(i["internal_name"].startswith("recipe_") for i in item_list)
    assert len(item_list) < len(items())

    patch_list = await read("patches://list")
    assert patch_list[-1]["name"] == patches()[-1].name

    assert await read("metrics://definitions") == METRIC_DEFINITIONS
    for metric in ("win_rate", "pick_rate", "advantage", "gpm", "xpm", "lane_outcome"):
        assert metric in METRIC_DEFINITIONS


async def test_detail_resources_by_id_and_name():
    by_id = await read("heroes://1")
    by_name = await read("heroes://antimage")
    assert by_id == by_name
    assert by_id["name"] == "Anti-Mage"
    assert by_id["data_patch"] == patches()[-1].name

    bkb = await read("items://bkb")
    assert bkb["name"] == "Black King Bar"
    assert await read(f"items://{bkb['id']}") == bkb


async def test_detail_resource_unknown_raises():
    with pytest.raises(MCPError):
        await read("heroes://notahero")
